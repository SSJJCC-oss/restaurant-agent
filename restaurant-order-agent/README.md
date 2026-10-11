# Restaurant Order Agent — v1 scaffold

WhatsApp ordering agent for the Order module, built on Google ADK + Gemini
(via Vertex AI), with Google Sheets as the data store and Twilio as the
WhatsApp gateway. See the project's `order-module-design.md` for the full
architecture this implements.

This is a **starting scaffold**, not a finished app — it gets you to the
first working milestone (talk to the agent locally, tool-calling working)
fast, with clear next steps for the rest.

## 0. Prerequisites (do these once, outside the code)

1. **Google Cloud project** with billing enabled. Enable two APIs:
   `gcloud services enable aiplatform.googleapis.com sheets.googleapis.com`
2. **A service account** for local dev, with the `Vertex AI User` role:
   ```
   gcloud iam service-accounts create restaurant-agent-dev
   gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
     --member="serviceAccount:restaurant-agent-dev@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
     --role="roles/aiplatform.user"
   gcloud iam service-accounts keys create service-account.json \
     --iam-account=restaurant-agent-dev@YOUR_PROJECT_ID.iam.gserviceaccount.com
   ```
   Put `service-account.json` in this folder (it's already gitignored).
3. **A Google Sheet** with three tabs — `Customers`, `Orders`, `Campaigns` —
   headed exactly as described in `order-module-design.md` section 3. Share
   the sheet with `restaurant-agent-dev@YOUR_PROJECT_ID.iam.gserviceaccount.com`
   as an Editor. Copy the spreadsheet ID out of its URL.
4. **A Twilio account** with the WhatsApp sandbox activated (Console →
   Messaging → Try it out → Send a WhatsApp message). Note your Account SID,
   Auth Token, and the sandbox WhatsApp number.

## 1. Local environment

```bash
cd whatsapp-order-agent
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`: your GCP project ID, the Sheets spreadsheet ID, and your
Twilio credentials. Leave `DRY_RUN=true` for now.

## 2. First milestone: talk to the agent, no Twilio needed

```bash
python scripts/test_agent_locally.py
```

Try a few messages as if you were a customer: `"hi, I'd like 2 chicken
biriyanis delivered to Al Wasl Road"`, or `"please remove me from your
list"`. Watch the terminal log lines from `app/tools.py` to see which tool
the agent picks — this is where you'll spend most of your early iteration,
tightening `app/agent.py`'s instructions until it's reliably choosing the
right tool with the right arguments. Nothing touches your Sheet yet
(`DRY_RUN=true`).

## 3. Wire up the real Sheet

Set `DRY_RUN=false` in `.env` and re-run step 2 — `app/tools.py` will now
call `app/sheets_client.py` for real. Check the Sheet updates as expected
after an order and after a "remove me" request.

## 4. Wire up Twilio

```bash
uvicorn app.webhook:app --reload --port 8080
```

In another terminal, expose it publicly (e.g. with [ngrok](https://ngrok.com)):

```bash
ngrok http 8080
```

In the Twilio console, set the sandbox's "WHEN A MESSAGE COMES IN" webhook to
`https://<your-ngrok-url>/twilio/whatsapp`. Message your Twilio sandbox
number from WhatsApp and watch it flow through.

## Known rough edges in this scaffold (by design — fix as you go)

- **ADK import paths/model name**: written against the standalone `google-adk`
  package as documented at the time this was generated. ADK's API has moved
  fast — if `app/agent.py` or the webhook don't import cleanly after
  `pip install google-adk`, check `pip show google-adk` and that version's
  own quickstart, since you already know this framework.
- **Session persistence**: `InMemorySessionService` is used everywhere,
  meaning conversation state (e.g. "waiting on this customer's address") is
  lost on every restart. Fine for local dev; needs a persistent
  `SessionService` before this goes near production (see design doc §6).
- **No campaign/broadcast sending yet** — this scaffold only covers inbound
  message handling. The Campaign Engine (Cloud Scheduler → template
  broadcast) is a separate piece, listed as a later build-order step.
- **Not deployed** — running locally only. Deploying to Cloud Run comes
  after the flows above are working end-to-end.

## Where this fits in the bigger build order

This scaffold covers build-order steps 1–4 from `order-module-design.md`
section 8. Next up: step 5 (deploy to Cloud Run) and step 6 (campaign engine).
