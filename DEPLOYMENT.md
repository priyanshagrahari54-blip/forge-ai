# FORGE AI — DEPLOYMENT
## Local
Install dependencies → configure environment → run tests → start service → smoke test.

## CI
Run tests and static checks; prevent secrets and regressions.

## Render
Use environment variables for secrets. Web process must bind to platform PORT. Verify health and task execution after deployment.

## Secrets
Never commit or print provider keys. Document variable names only.

## Production verification
Deployment status alone is insufficient. Verify health, authentication, real inference, task creation, worker execution and representative E2E behavior.
