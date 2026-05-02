# Sentinel — ML models (V1)

Three classical-ML stages.  All three default to **pure-Python
heuristics** so the Docker image stays small.  Optional sklearn /
xgboost / onnxruntime upgrades kick in automatically when those
packages are installed.

## SecretDetector

* **Default backend:** regex catalogue + Shannon-entropy fallback.
* **Optional backend:** scikit-learn `RandomForestClassifier` (when
  `scikit-learn` is installed and a model pickle exists at
  `document_processor/sentinel/data/secret_detector.pkl`).
* **Training data:** the user supplies labelled positives / negatives
  via `amor sentinel train-secrets` (V1.1).
* **Heuristic catalogue (12 patterns):**
  * AWS access key (`AKIA…`)
  * AWS secret-key heuristic (40-char base64 near `aws_secret`)
  * GCP service-account JSON
  * Stripe live/test keys (`sk_(live|test)_…`)
  * GitHub PAT / GitHub App token (`ghp_…` / `ghu_…` / `ghs_…`)
  * Slack bot/user tokens (`xox[abprs]-…`)
  * OpenAI / Anthropic keys
  * RSA / OPENSSH / EC private key blocks
  * JWT (3-segment base64-url)
  * Bearer / Authorization headers
  * Inline `password = "…"` / `api_key = "…"` literals

Confidence is dampened on test paths (`tests/`, `test_`, `fixture`,
`example`) so a fixture secret doesn't crowd out real ones.

## AnomalyDetector

* **Default backend:** per-file Z-score on
  `(loc, complexity_proxy, imports, base64_density)`.  Files with
  any axis where `|z| > threshold` (default 3.0) get a low-severity
  finding.
* **Optional backend:** scikit-learn `IsolationForest` when
  `scikit-learn` is installed and the caller passes
  `backend="sklearn"` to the constructor.

## SeverityRanker

* **Default backend:** weighted-sum heuristic
  ```
  score = 0.55 * severity_rank + 0.30 * source_weight
        + 0.15 * path_risk_multiplier
  ```
  Path-risk hints boost auth/login/payment/admin paths.
* **Never downgrades** below the original tool's claim — gitleaks
  said "critical", we keep "critical" no matter what.
* **Optional backend:** `XGBClassifier` when `xgboost` is installed.
  V1 ships only the heuristic.

## Embedder

* **Default backend:** deterministic 96-dim hash sketch (the same
  one Quick Code V2's Striatum uses) — works offline with zero deps.
* **Production backend:** `sentence-transformers` with
  `nomic-ai/nomic-embed-text-v1.5` on CPU.  Reuses the existing
  `local_ai/vector_store/lancedb_store.py` adapter.

## Active backend reporting

`MLPipeline.backend_summary()` returns a dict that the engine logs
at the start of every scan and surfaces via SSE:

```json
{
  "secret_detector": "heuristic",
  "anomaly_detector": "heuristic",
  "severity_ranker": "heuristic"
}
```

When the user `pip install scikit-learn xgboost`, those values flip
to `"sklearn"` / `"xgboost"` automatically with no code change.

## Retraining

V1 ships only the heuristic baseline.  V1.1 will add:

* `amor sentinel train-secrets` — train RF on user-labelled
  positives + the bundled negative corpus.
* Confidence calibration sink — when the user marks a finding as
  false positive, its feature vector lands in
  `sentinel_models_calibration` Mongo collection; the next training
  run picks it up.
