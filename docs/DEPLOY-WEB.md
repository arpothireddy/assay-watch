# Deploy: the web app (MCP server + website)

This is the one-time setup for `assay-watch-mcp` and `assay-watch-web` --
assumes the base project setup from [`docs/DEPLOY.md`](DEPLOY.md) (project,
APIs, Artifact Registry, Cloud Build↔GitHub connection) already exists, since
this reuses all of it.

```
push to main
     │
     ▼
cloudbuild-web.yaml (auto)
  build both images → deploy to STAGING → smoke test
     │
     ├─ fail → stop. Nothing else happens.
     ▼ pass
  tag "staging-verified", fire the promote trigger
     │
     ▼
cloudbuild-web-promote.yaml -- sits PENDING APPROVAL
     │
     ▼  you approve (Cloud Build console, or `gcloud builds approve`)
  deploys the exact verified images to PRODUCTION
```

Public endpoints on both services (`--allow-unauthenticated`) -- a
deliberate simplification: `assay-watch-mcp` only ever serves read-only
watch-listing data (no secrets, no write path), so skipping
service-to-service IAM auth between it and `assay-watch-web` avoids real
code complexity (fetching identity tokens) for a security property that
doesn't buy much here. `assay-watch-web` obviously has to be public --
it's the website.

## 1. New Postgres role and secrets (if you haven't already)

You should already have these from earlier in this build -- confirm rather
than repeat:
```bash
gcloud secrets describe assay-watch-reader-db-url
gcloud secrets describe assay-watch-gemini-api-key
```
If either is missing, see the conversation history for the `assay_reader`
role SQL and the secret-creation commands.

## 2. Additional IAM for the existing Cloud Build service account

The crawler's Cloud Build SA already has `roles/run.developer` (covers
Cloud Run *Services* too, not just Jobs) and `roles/artifactregistry.writer`.
It needs two more things for this pipeline:

```bash
PROJECT_NUMBER=$(gcloud projects describe assay-watch --format='value(projectNumber)')
CLOUDBUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

# Act as the two new runtime service accounts when deploying:
gcloud iam service-accounts add-iam-policy-binding assay-watch-mcp-runtime@assay-watch.iam.gserviceaccount.com \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/iam.serviceAccountUser"
gcloud iam service-accounts add-iam-policy-binding assay-watch-web-runtime@assay-watch.iam.gserviceaccount.com \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/iam.serviceAccountUser"

# Invoke the promote trigger from within the staging pipeline's last step:
gcloud projects add-iam-policy-binding assay-watch \
  --member="serviceAccount:${CLOUDBUILD_SA}" --role="roles/cloudbuild.builds.editor"
```

## 3. Create the two triggers

**Staging (normal, fires on every push to main)** -- same pattern as the
crawler's trigger:
```bash
gcloud builds triggers create github \
  --repo-name=assay-watch --repo-owner=arpothireddy \
  --branch-pattern='^main$' \
  --build-config=cloudbuild-web.yaml \
  --name=assay-watch-web-deploy
```

**Promote (manual invocation only, requires approval)** -- this is the one
step I'm genuinely less certain about the exact current CLI syntax for
(`gcloud builds triggers create manual` is a newer trigger type and its
flags have moved between gcloud releases). Try this first:

```bash
gcloud builds triggers create manual \
  --name=assay-watch-web-promote \
  --repo=https://github.com/arpothireddy/assay-watch \
  --repo-type=GITHUB \
  --branch=main \
  --build-config=cloudbuild-web-promote.yaml \
  --require-approval
```

If that errors on flag names, the **console path is more reliable** and
takes the same effect: Cloud Build → Triggers → **Create Trigger** →
Repository: `assay-watch` (already connected) → Event: **Manual
invocation** → Configuration: **Cloud Build configuration file** →
`cloudbuild-web-promote.yaml` → check **Require approval** → name it
`assay-watch-web-promote` (must match exactly -- `cloudbuild-web.yaml`'s
`_PROMOTE_TRIGGER` substitution references this name) → Create.

Either way, verify afterward:
```bash
gcloud builds triggers describe assay-watch-web-promote --format="value(approvalConfig.approvalRequired)"
```
Should print `True`.

## 4. First deploy

```bash
git log -1 --oneline   # confirm you're on the latest pushed commit; nothing to do if so
```

The push that landed the pipeline files already fired `assay-watch-web-deploy`
(triggers watch `main` from the moment they're created, but only against
commits pushed *after* creation -- if nothing fired, push a trivial commit
or run it by hand):
```bash
gcloud builds triggers run assay-watch-web-deploy --branch=main
```

Watch it:
```bash
gcloud builds list --limit=1 --format="table(id,status,createTime)"
```

## 5. Approve the promotion

Once staging passes, find the pending promote build:
```bash
gcloud builds list --filter="status=PENDING_APPROVAL" --limit=1 --format="table(id,createTime)"
```
Look at what it's about to do (worth actually reading before approving):
```bash
gcloud builds describe <build-id> --format="value(substitutions)"
```
Approve it:
```bash
gcloud builds approve <build-id>
```
Or do the same from the Cloud Build console -- open the build, click
**Approve**.

## Testing checklist

```bash
# Staging URLs:
gcloud run services describe assay-watch-web-staging --region=us-central1 --format='value(status.url)'
gcloud run services describe assay-watch-mcp-staging --region=us-central1 --format='value(status.url)'

# After approval, production URLs:
gcloud run services describe assay-watch-web --region=us-central1 --format='value(status.url)'
```

Open the web URL in a browser and actually search something. Or via curl:
```bash
curl -s -X POST "<web-url>/api/search" -H "Content-Type: application/json" \
  -d '{"query": "black rolex dive watch"}' | python3 -m json.tool
```

To see the rollback path work: push a commit that breaks the smoke test
(e.g. a typo in the health check path) and confirm the staging pipeline
fails *before* ever reaching the promote step -- production stays on
whatever was last approved.
