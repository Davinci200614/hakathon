# Public pages and HTTPS deployment

## Content review before public use

The French pages match the existing dashboard language. `/privacy`, `/terms` and
`/data-deletion` render without backend requests or credentials. The shared footer
links to all three. The existing dashboard remains at `/`.

Content was checked against `backend/app/{db,service,ai,whatsapp,main}.py` and the
actual SQLite database schema, read-only, without reading farmer records. No
backend code, webhook settings or Meta credentials were changed.

The owner must confirm and replace the clearly marked provisional information:

- Operator/data controller identity and actual contact details.
- A monitored deletion/contact channel and the person responsible for responding.
- Retention and backup/log deletion rules; none are automated in the current app.
- Hosting provider, locations, provider account settings and international transfers.
- Applicable legal bases, rights/complaint process and commercial terms.

There is intentionally no fake email address, phone number, submission form,
promised deadline or claim that the WhatsApp bot handles deletion requests.

## Manual deletion request handling

1. Establish a monitored human contact and publish it on all relevant pages.
2. Receive the template from `/data-deletion`; acknowledge receipt manually.
3. Verify control of the relevant WhatsApp number proportionately. Never request
   login codes. If the number is no longer accessible, assess an alternative
   verification method before disclosing or deleting records.
4. With authorized database access, locate `farmers.phone`,
   `messages.farmer_phone` and `requests.farmer_phone` using the verified number.
   Also locate `conversations.farmer_phone` and `conversation_turns.farmer_phone`.
   Locate `bookings.request_id` associated with those requests. Do not export
   unrelated farmer data. These are the current schema relationships; SQLite
   does not enforce foreign keys here.
5. Review scope and any legitimate retention requirements. If deleting, perform
   the related booking, request, conversation, conversation-turn, message and farmer deletions in one database
   transaction, using parameterized queries. Check that no matching active
   records remain. The app has no deletion endpoint or admin action today.
6. Review server logs, backups and any exports separately. Avoid restoring
   deleted records from backups; define a process for this. Assess provider-side
   requests where applicable; local deletion does not erase Meta/Google Gemini copies.
7. Send a human confirmation specifying what was removed, what remains and why.
   Keep only a proportionate record of the request under an owner-approved rule.

No deletion was executed while implementing these pages.

## Build and preview locally

From `nouba-agriflow/frontend`:

```bash
npm ci
npm run build
npm exec vite -- preview --host 127.0.0.1 --port 4173
```

Open each route directly, refresh it, and follow the footer links:

- http://127.0.0.1:4173/privacy
- http://127.0.0.1:4173/terms
- http://127.0.0.1:4173/data-deletion

## Netlify: publish the frontend only

1. Build using the commands above.
2. In Netlify, use its manual deployment upload and upload **only `frontend/dist`**.
   Alternatively connect the repository, set the base directory to
   `nouba-agriflow/frontend` (or `frontend` if the repository root is
   `nouba-agriflow`), build command `npm run build`, publish directory `dist`.
3. `public/_redirects` is copied to `dist/_redirects` and rewrites the three public
   URLs to the React entry point. Keep it in the deployed folder so direct links
   and refreshes work. Other hosts need equivalent rewrites.
4. Use the assigned `https://<site-name>.netlify.app` address. Netlify provisions
   HTTPS automatically. For a custom domain, configure its DNS in Netlify and
   check **Domain management → HTTPS** for certificate status.
5. Recheck all three deployed URLs on desktop and mobile, including direct loads
   and refreshes. Confirm that they are accessible without login.

Do not upload `backend`, `.env`, SQLite databases or the project root. No Meta or
Google Gemini secrets belong in a frontend environment variable or client bundle.

The Vite development `/api` proxy is not included in a static deployment. The
public information pages work independently, but the dashboard at `/` requires
separate API hosting and routing. Do not expose the current unauthenticated API
or add a public proxy to it merely to publish these pages. Backend authentication
and deployment are separate work; this change does not alter the webhook URL.

Official deployment references:

- https://docs.netlify.com/build/frameworks/framework-setup-guides/vite/
- https://docs.netlify.com/manage/domains/secure-domains-with-https/https-ssl/

## Verification performed in the implementation environment

- `npm run build` passed (TypeScript and Vite production bundle).
- All three page components rendered successfully with React server rendering;
  checked page headings, owner-confirmation notices, active navigation and links.
- Checked policy provider/storage disclosures and the deletion request template.
- Checked dashboard footer integration and all three built `_redirects` rules.
- A live preview/browser check could not run: binding `127.0.0.1:4173` is denied
  with `EPERM` in this sandbox. No visual browser/mobile verification is claimed.
  Use the local commands above to complete direct-load/refresh and mobile checks.
