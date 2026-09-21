#!/usr/bin/env bash
# Creates an isolated namespace for Nightorder Argo e2e tests.
# Touches NOTHING shared: own namespace, own Role/RoleBinding on its default
# SA. Tear down with: kubectl --context "$CTX" delete ns nightorder-e2e
set -euo pipefail

CTX="${NIGHTORDER_KUBE_CONTEXT:-my-staging-cluster}"
NS="nightorder-e2e"

kubectl --context "$CTX" apply -f - <<EOF
apiVersion: v1
kind: Namespace
metadata:
  name: ${NS}
  labels:
    app.kubernetes.io/managed-by: nightorder
    purpose: nightorder-e2e-testing
---
# Argo >=3.5 emissary executor: workflow pods must report task results.
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: nightorder-workflow-executor
  namespace: ${NS}
rules:
  - apiGroups: ["argoproj.io"]
    resources: ["workflowtaskresults"]
    verbs: ["create", "patch"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: nightorder-workflow-executor
  namespace: ${NS}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: nightorder-workflow-executor
subjects:
  - kind: ServiceAccount
    name: default
    namespace: ${NS}
EOF

echo "namespace ${NS} ready on ${CTX}"
