# Deploying MailMatrixAI to Kubernetes

These manifests run the MailMatrixAI web app from the container image published
to the GitHub Container Registry (GHCR) by
[`.github/workflows/docker-publish.yml`](../.github/workflows/docker-publish.yml).

```
k8s/
├── secret.example.yaml   # credentials template → copy to secret.yaml
├── pvc.yaml              # persistent storage for rules + summaries
├── deployment.yaml       # the app pod
├── service.yaml          # ClusterIP service
└── ingress.yaml          # OPTIONAL external access (needs auth + TLS)
```

> **Security:** the app has **no built-in login**. Anyone who can reach it can
> read and send your mail. Keep it on a `ClusterIP` Service and use
> `kubectl port-forward`, or put it behind an authenticating proxy + TLS
> (see `ingress.yaml`). Never expose it to the internet unprotected.

---

## Prerequisites

- A Kubernetes cluster and `kubectl` pointed at it (`kubectl cluster-info`).
- A default `StorageClass` for the PVC (`kubectl get storageclass`). If the
  default isn't the one you want, set `storageClassName:` in `pvc.yaml`.
- A Fastmail app password and an Anthropic API key.

---

## The image

The workflow publishes to **`ghcr.io/cs4p/mailmatrixai`** on every push to
`main` (tag `latest` + a `sha-` tag) and on every `vX.Y.Z` git tag (semver tags).

- **Public package** (default once you make it public): no pull secret needed.
- **Private package:** create a pull secret and reference it (see
  [Pulling a private image](#pulling-a-private-image) below).

Build and push manually instead of via CI:

```bash
# From the repo root
docker build -t ghcr.io/cs4p/mailmatrixai:latest .
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <github-user> --password-stdin
docker push ghcr.io/cs4p/mailmatrixai:latest
```

Run it locally to sanity-check before deploying:

```bash
docker run --rm -p 5000:5000 \
  -e IMAP_SERVER=imap.fastmail.com -e IMAP_PORT=993 \
  -e IMAP_USERNAME=you@example.com -e IMAP_PASSWORD=app-password \
  -e SMTP_SERVER=smtp.fastmail.com -e SMTP_PORT=465 \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  ghcr.io/cs4p/mailmatrixai:latest
# open http://localhost:5000
```

---

## Deploy

All commands assume you're in the repo root. Add `-n <namespace>` (and create it
with `kubectl create namespace <namespace>`) if you don't want the `default`
namespace.

### 1. Create the credentials Secret

```bash
cp k8s/secret.example.yaml k8s/secret.yaml   # secret.yaml is git-ignored
# edit k8s/secret.yaml with your real IMAP/SMTP/Anthropic values
kubectl apply -f k8s/secret.yaml
```

Or create it imperatively without a file:

```bash
kubectl create secret generic mailmatrixai-credentials \
  --from-literal=IMAP_SERVER=imap.fastmail.com \
  --from-literal=IMAP_PORT=993 \
  --from-literal=IMAP_USERNAME=you@example.com \
  --from-literal=IMAP_PASSWORD=your_app_password \
  --from-literal=SMTP_SERVER=smtp.fastmail.com \
  --from-literal=SMTP_PORT=465 \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...
```

### 2. Create storage and workload

```bash
kubectl apply -f k8s/pvc.yaml
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

### 3. Verify

```bash
kubectl rollout status deployment/mailmatrixai
kubectl get pods -l app=mailmatrixai
kubectl logs -l app=mailmatrixai -f
```

### 4. Reach the app

```bash
kubectl port-forward service/mailmatrixai 5000:80
# open http://localhost:5000
```

For persistent external access, edit `k8s/ingress.yaml` (set your host, TLS
secret, and an auth mechanism) and `kubectl apply -f k8s/ingress.yaml`.

---

## Updating

```bash
# after CI publishes a new :latest (imagePullPolicy is Always)
kubectl rollout restart deployment/mailmatrixai

# or pin to an immutable tag
kubectl set image deployment/mailmatrixai mailmatrixai=ghcr.io/cs4p/mailmatrixai:v0.3.0
```

## Pulling a private image

If the GHCR package is private:

```bash
kubectl create secret docker-registry ghcr-pull \
  --docker-server=ghcr.io \
  --docker-username=<github-user> \
  --docker-password=<github-token-with-read:packages>

# add to deployment.yaml under spec.template.spec:
#   imagePullSecrets:
#     - name: ghcr-pull
```

## How state is stored

`emailRules.json` (learned filing rules) and the generated HTML summaries are
written to `MAILMATRIX_DATA_DIR` (`/data` in the image), backed by the
`mailmatrixai-data` PVC — so they survive pod restarts. Credentials come only
from the Secret; nothing sensitive is written to the volume.

## Teardown

```bash
kubectl delete -f k8s/service.yaml -f k8s/deployment.yaml
kubectl delete -f k8s/pvc.yaml          # deletes stored rules + summaries
kubectl delete secret mailmatrixai-credentials
```
