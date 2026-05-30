# OrchestrAi — landing page

Standalone marketing site. Pure static HTML/CSS, no build step.

## Run / preview
Open `index.html` directly in a browser, or serve the folder:

```bash
cd landing && python -m http.server 8088   # then http://localhost:8088
```

## Deploy
Any static host (Netlify, Cloudflare Pages, S3, nginx). It's intentionally
decoupled from the hub so it can live on the apex domain while the app lives
at e.g. `app.<domain>`.

## TODO (tomorrow)
- Drop in real branding: logo, colour palette, fonts, product screenshots.
- Point the "Sign in" / CTA links at the real app URL (currently relative `/`,
  which only works when co-hosted with the hub).
- Confirm final copy + pricing wording with marketing.
