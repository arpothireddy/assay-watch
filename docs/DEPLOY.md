# Deploy: Cloud Build + Cloud Run Jobs + Cloud Scheduler (free tier)

This replaces the old self-hosted-runner deploy (`deploy.yml` on a VM) with a
fully managed, effectively-$0/month pipeline:

```
push to main on GitHub
        |
        v
  Cloud Build  ──build──>  Artifact Registry  ──deploy──>  Cloud Run Jobs
   (trigger)                (image storage)         (assay-watch-migrate,
                                                       assay-watch-crawl)
                                                              ^
                                                              |
                                                      Cloud Scheduler
                                                    (fires crawl daily)

  Postgres lives on Neon (free tier), not on GCP.
```

Why these pieces:

- **Cloud Run Jobs**, not Cloud Run *services* -- this is a batch crawler with
  no HTTP endpoint, and Jobs is the right primitive for "run this container to
  completion, on a schedule."
- **Cloud Build** builds the image and does the deploy in one pipeline, on
  every push to `main`. Free tier: 120 build-minutes/day -- this pipeline
  takes a few minutes, so daily/per-push builds cost nothing.
- **Cloud Scheduler** is the free-tier replacement for the systemd timer /
  cron the self-hosted deploy assumed. It just calls the Cloud Run Jobs API on
  a cron schedule.
- **Neon** for Postgres. Cloud SQL has no perpetual free tier (it bills for
  instance uptime regardless of traffic); Neon's free tier gives a public,
  SSL-terminated Postgres endpoint that Cloud Run can reach directly, no VPC
  connector needed.

Expected steady-state cost: **$0/month**, all four GCP services and Neon
included, at this project's traffic (one crawl run/day, a few minutes of
compute).

---

## 0. Prerequisites

- The `gcloud` CLI, installed and authenticated: `gcloud auth login`.
- A Neon account (https://neon.tech -- sign up free, no card required for the
  free tier).
- Push access to `arpothireddy/assay-watch` on GitHub (you already have this).

Merge `claude/phase0-spine` into `main` before starting -- `cloudbuild.yaml`
and the app code need to be on `main` for the trigger (step 6) to build them.

---

## 1. Create the GCP project and enable billing

Billing must be enabled even to use free-tier resources (GCP requires a
billing account on the project, it just won't charge you within the free
allowances). New accounts get a $300/90-day credit as a safety net, but
nothing here should touch it.

```bash
PROJECT_ID="assay-watch-$(date +%s | tail -c 6)"   # must be globally unique
gcloud projects create "$PROJECT_ID" --name="assay-watch"
gcloud config set project "$PROJECT_ID"

# List your billing accounts, then link one:
gcloud billing accounts list
gcloud billing projects link "$PROJECT_ID" --billing-account=BILLING_ACCOUNT_ID
```

Keep a terminal with `PROJECT_ID` exported for the rest of this doc:

```bash
export PROJECT_ID="$PROJECT_ID"
export REGION="us-central1"   # matches cloudbuild.yaml's default
```

## 2. Enable the APIs you'll need

```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  iam.googleapis.com
```

## 3. Artifact Registry: create the image repo

```bash
gcloud artifacts repositories create assay-watch \
  --repository-format=docker \
  --location="$REGION" \
  --description="assay-watch crawler images"
```

Free tier is 0.5 GB storage; overage is ~$0.10/GB/month, so even if this
repo grows past it, it's pocket change, not a real cost. Optional cleanup
(keep the 5 most recent images) if you want to stay under it indefinitely:

```bash
cat > /tmp/cleanup-policy.json <<'EOF'
[
  {
    "name": "keep-recent",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": 5}
  }
]
EOF
gcloud artifacts repositories set-cleanup-policies assay-watch \
  --location="$REGION" --policy=/tmp/cleanup-policy.json
```

(`known-good` and `latest` tags always point at one of the recent digests, so
this won't delete anything you're actually using. If the flag above has moved
in your gcloud version, skip it -- it's housekeeping, not load-bearing.)

## 4. Neon: create the project, database, and app role

1. In the Neon console, create a project (any region close to `us-central1`,
   e.g. AWS `us-east-2` or GCP `us-central1` if offered).
2. Note the connection details Neon shows you for the default role -- this
   role plays the **migrator** (schema owner) in this app's model. You'll get
   something like:
   ```
   postgresql://neondb_owner:AbCdEf123@ep-xxxx.us-east-2.aws.neon.tech/neondb?sslmode=require
   ```
3. Open the Neon SQL editor (connected as that default role) and run
   [`scripts/db-init/neon-app-role.sql`](../scripts/db-init/neon-app-role.sql)
   after filling in a generated password and your database name. This creates
   the least-privilege `assay_app` login role -- the same role split the
   self-hosted docker-compose stack uses (migrator has DDL rights; app can
   only `INSERT`/`SELECT`, and `UPDATE` on `crawl_runs` only -- migration
   `0001` grants those table privileges automatically the first time it runs).
4. Build the two connection strings the app needs (note `sslmode=require` --
   Neon requires TLS):
   ```
   ASSAY_MIGRATOR_DATABASE_URL=postgresql+psycopg://neondb_owner:<password>@<host>/<db>?sslmode=require
   ASSAY_DATABASE_URL=postgresql+psycopg://assay_app:<password>@<host>/<db>?sslmode=require
   ```
   Note the `+psycopg` driver segment -- `settings.py` builds URLs in this
   form; keep it when you paste Neon's plain `postgresql://` string in.

## 5. Secret Manager: store the two connection strings

```bash
printf '%s' "$ASSAY_MIGRATOR_DATABASE_URL" | \
  gcloud secrets create assay-watch-migrator-db-url --data-file=-
printf '%s' "$ASSAY_DATABASE_URL" | \
  gcloud secrets create assay-watch-app-db-url --data-file=-
```

(Set the two env vars in your shell first, or just paste the values directly
in place of `$ASSAY_...` -- either way, avoid leaving them in shell history if
this is a shared machine: `history -d` the line afterward, or use a file with
`--data-file=path` instead of a literal on the command line.)

## 6. Service accounts and IAM

**Runtime SA** -- what the Cloud Run Jobs run as; needs to read the two
secrets:

```bash
gcloud iam service-accounts create assay-watch-runtime \
  --display-name="assay-watch Cloud Run runtime"

for secret in assay-watch-migrator-db-url assay-watch-app-db-url; do
  gcloud secrets add-iam-policy-binding "$secret" \
    --member="serviceAccount:assay-watch-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor"
done
```

**Cloud Build's own SA** -- needs to push images, deploy/execute Cloud Run
Jobs, and act as the runtime SA when deploying:

```bash
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/run.developer"
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/artifactregistry.writer"
gcloud iam service-accounts add-iam-policy-binding \
  "assay-watch-runtime@${PROJECT_ID}.iam.gserviceaccount.com" \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/iam.serviceAccountUser"
```

## 7. Connect GitHub and create the Cloud Build trigger

This part is console-driven (GitHub App authorization isn't scriptable):

1. Console -> Cloud Build -> Triggers -> **Connect Repository**.
2. Choose GitHub (Cloud Build GitHub App), authorize it, select
   `arpothireddy/assay-watch`.
3. Create trigger:
   - Event: **Push to a branch**
   - Branch: `^main$`
   - Configuration: **Cloud Build configuration file** -> `cloudbuild.yaml`
   - Service account: leave as the default Cloud Build SA (the one you just
     granted roles to in step 6).

Or non-interactively once the repo connection exists:

```bash
gcloud builds triggers create github \
  --repo-name=assay-watch --repo-owner=arpothireddy \
  --branch-pattern='^main$' \
  --build-config=cloudbuild.yaml \
  --name=assay-watch-deploy
```

## 8. Cloud Scheduler: trigger the daily crawl

The scheduler calls the Cloud Run Jobs API directly (no Pub/Sub needed). It
needs its own service account with permission to run just that one job:

```bash
gcloud iam service-accounts create assay-watch-scheduler \
  --display-name="assay-watch Cloud Scheduler invoker"

gcloud run jobs add-iam-policy-binding assay-watch-crawl \
  --region="$REGION" \
  --member="serviceAccount:assay-watch-scheduler@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

gcloud scheduler jobs create http assay-watch-daily-crawl \
  --location="$REGION" \
  --schedule="0 6 * * *" \
  --time-zone="UTC" \
  --uri="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/assay-watch-crawl:run" \
  --http-method=POST \
  --oauth-service-account-email="assay-watch-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
```

(`assay-watch-crawl` must already exist for `add-iam-policy-binding` to
succeed -- it's created by the first Cloud Build run in step 9. Do step 9
before this one if you're going in strict order.) Adjust the cron schedule /
time zone to taste; `0 6 * * *` UTC is "once a day."

## 9. First deploy

```bash
git checkout main && git merge claude/phase0-spine && git push origin main
```

This fires the trigger from step 7. Watch it:

```bash
gcloud builds list --ongoing
gcloud builds log --stream $(gcloud builds list --ongoing --format='value(id)' --limit=1)
```

First run creates both Cloud Run Jobs, applies migration `0001` to your empty
Neon database, then does a dry-run smoke test. If it fails, the build log
tells you which step, and the crawl job has nothing to roll back to yet (first
deploy) -- fix and push again.

---

## Testing checklist

Once the first deploy is green:

**1. Trigger a build manually** (without waiting for a push):
```bash
gcloud builds triggers run assay-watch-deploy --branch=main
```

**2. Confirm both jobs exist and point at the new image:**
```bash
gcloud run jobs list --region="$REGION"
```

**3. Run a real crawl by hand** (not the scheduler, not a dry run):
```bash
gcloud run jobs execute assay-watch-crawl --region="$REGION" --wait
```

**4. Check the execution's logs:**
```bash
gcloud run jobs executions list --job=assay-watch-crawl --region="$REGION" --limit=5
gcloud logging read \
  'resource.type="cloud_run_job" AND resource.labels.job_name="assay-watch-crawl"' \
  --limit=50 --format='value(textPayload)'
```

**5. Confirm data landed**, in the Neon SQL editor:
```sql
select count(*) from listing_snapshots;
select source, status, listings_found, started_at from crawl_runs order by started_at desc limit 5;
```

**6. Confirm the scheduler is wired correctly** (fire it now instead of
waiting for 06:00 UTC):
```bash
gcloud scheduler jobs run assay-watch-daily-crawl --location="$REGION"
```

**7. Exercise the rollback path** (optional, but this is the one thing that's
hard to trust without seeing it happen): push a commit that breaks the
migration or the dry-run crawl, watch the Cloud Build log show the failure and
the automatic `gcloud run jobs update ... --image=...known-good` rollback, then
confirm `assay-watch-crawl` still points at the last good image:
```bash
gcloud run jobs describe assay-watch-crawl --region="$REGION" --format='yaml(spec.template)'
```

**8. Set a budget alert** (belt-and-suspenders, since "should be free" isn't
the same as "will never bill"):
```bash
gcloud billing budgets create --billing-account=BILLING_ACCOUNT_ID \
  --display-name="assay-watch guardrail" \
  --budget-amount=5USD \
  --threshold-rule=percent=1.0
```
