# n8n Orchestration for Lazarus-CI

n8n serves as the external **intake, notification, and escalation bridge** between GitHub Actions / CI webhooks and the Lazarus SRE engine.

## 1. Quick Start with Docker
```bash
docker run -d --name lazarus-n8n -p 5678:5678 -e N8N_SECURE_COOKIE=false \
  --add-host=host.docker.internal:host-gateway -v n8n_data:/home/node/.n8n n8nio/n8n
```

## 2. Import Workflow
1. Open [http://localhost:5678](http://localhost:5678)
2. Click **Add workflow** -> **Import from File...**
3. Select `n8n/lazarus_workflow.json`
4. Configure your Telegram Bot credential or webhook secret
5. Toggle workflow to **Active**

## 3. Triggering from CI / Webhook
```bash
curl -X POST http://localhost:5678/webhook/lazarus-alert \
  -H "Content-Type: application/json" \
  -d '{"repository": "org/sample-app", "failed_stage": "build"}'
```
