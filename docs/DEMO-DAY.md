# Demo day

Everything here exists because something on this list has already failed once during
development. Read it the morning of, not during.

---

## 0. The deployed URLs — this is what you actually present

* **Frontend (Vercel):** `https://medi-kiosk-fe.vercel.app` — static build of the Vite app
  (Vercel project `medi-kiosk-fe`). `medikiosk.vercel.app`, the name first requested, belongs to
  someone else's login-protected project; do not send people there.
* **Backend (Render, Docker, free tier):** `https://medikiosk-api-docker.onrender.com` —
  built from the repo's own `Dockerfile`, so Tesseract is the same binary that was tested
  locally, not a substitute. Confirmed via `/about`: `isSupabase: true`, both OCR backends
  report `available: true`.
* **Database:** Supabase, session-mode pooler — the same one local development uses. Render's
  outbound network reaches it over IPv4, same as the pooler was chosen for in the first place.

**COLD START IS REAL ON THE FREE TIER.** Render spins the backend down after **15 minutes**
idle. A sleeping service does not answer slowly, it REFUSES: measured 2026-09-29, the first
request after 17 idle minutes came back instantly as `429 Too Many Requests` (plain text,
`x-render-routing: hibernate-rate-limited`), and the service was up ~33 seconds later. The
frontend now treats that as "waking": it shows the wake banner and retries for up to 90 seconds
(`shared/api.ts`), so a patient no longer sees "Something went wrong at our end" for it.

> **WARM THE BACKEND ~2 MINUTES BEFORE PRESENTING** by hitting `/about`:
>
> ```bash
> curl https://medikiosk-api-docker.onrender.com/about
> ```

Do this *again* right before walking up — a second gap (a judge's question, a room change) is
enough for it to sleep again on the free tier.

**THE EVIDENCE CROP TAKES ~5 SECONDS THE FIRST TIME.** Opening a document-sourced line in
the brief rasterises the PDF page server-side, and on free-tier CPU that measured 4.8s in
production (61KB PNG, HTTP 200 — it is working, not hanging). It is cached per page after
that. If you are demonstrating click-to-source, open one document line during the warm-up so
the second one is instant in front of the audience.

If you forget, the product does not lie about it: the first slow request raises a
"Waking the server… this can take up to a minute on first load" notice, so a cold boot reads as
a wait rather than a crash. It clears itself the moment the response lands — on the real
response, not a timer. That is a safety net, not a substitute for warming it: a minute of dead
air in front of judges is still a minute.

The frontend calls `/api/*`, `/about`, and `/mock-idp/*` as relative paths; `vercel.json`
rewrites those to the Render URL, so the browser never talks cross-origin and there is nothing
to reconfigure per-environment. CORS on the Render service is locked to the Vercel origin only
— a stray `curl` from elsewhere gets a database and API that work, but a browser tab pointed at
any other origin does not.

`DEMO_LOCAL_DB` plays no part in the deployed path. It stays what it always was: the
offline-presentation fallback for §2 below, clearly badged, never silently substituted for the
real thing.

---

## 1. Check the network can reach a database — before you present

Outbound port 5432 is blocked on a great many venue networks: conference NAT, hotel Wi-Fi,
corporate egress filtering. When it is, **no** Supabase endpoint works — not the direct one,
not the pooler. Both failure modes were hit during development within a single working session.

```bash
# Does this network allow Postgres out at all? (an unrelated host that accepts any port)
python3 -c "import socket; socket.create_connection(('portquiz.net', 5432), timeout=8)"
```

* **No error** → the network is fine. Use Supabase.
* **TimeoutError** → 5432 is blocked. Use the local database (§2). No connection string will
  help, and the application will refuse to start rather than pretend — by design, with the
  endpoint named, within about a minute.

Also check the endpoint itself resolves:

```bash
python3 -c "import socket; print(socket.getaddrinfo('db.<ref>.supabase.co', 5432))"
```

`gaierror [Errno 8]` while `dscacheutil -q host -a name db.<ref>.supabase.co` still shows an
`ipv6_address` means the DNS record exists but this machine has **no usable IPv6 route**. The
direct endpoint is IPv6-only. Use the session pooler — that is what `DATABASE_URL` should
already point at.

---

## 2. The local database, if the network is against you

```bash
make demo-local-up                     # Postgres 17 on 5433, migrated
DEMO_LOCAL_DB=true make demo           # start the stack against it
```

It is **never automatic**. If Supabase is unreachable and the flag is off, the app refuses to
start. That is deliberate: a silent switch is how someone ends up telling a room they are
showing the hosted project while looking at localhost.

When it is on, everything says so — a warning in the startup log, `database.isLocalDemo` in
`/about`, and a rose hazard-striped badge across the top of every screen. **Do not present
local data as Supabase.** The badge is there to make that impossible to do by accident.

```bash
make demo-local-down     # stop, keep the data
make demo-local-reset    # stop and delete the volume
```

---

## 3. The camera

`getUserMedia` requires a **secure context**. That means `https://` or `localhost` — nothing
else, in any browser.

### Presenting from the host machine — supported, nothing to do

`http://localhost:5173` is a secure context by definition. The camera works with no
certificate. This is the tested path.

The laptop webcam has no rear camera, so `facingMode: environment` falls back to the only
camera present. That is handled and is not an error.

### Presenting from a phone — DOCUMENTED, NOT BUILT

Reaching the dev server from a phone means `http://192.168.x.x:5173`, which is **not** a
secure context, so `getUserMedia` is simply absent and the kiosk shows its
"choose a photo instead" state. That state is correct and the encounter is not blocked — but
there is no camera.

Making it work needs a trusted certificate. This has **not been built or tested here**:

1. `brew install mkcert && mkcert -install && mkcert 192.168.x.x`
2. Serve Vite over HTTPS on `0.0.0.0` with that key/cert (`server.https`, `server.host`).
3. Install and trust the mkcert root CA **on the phone** (Settings → General → VPN & Device
   Management on iOS, then enable full trust in About → Certificate Trust Settings).
4. Point the API's CORS at the same origin.

Known ways this still fails on the day: many venue access points enable **client isolation**,
so the phone cannot reach the laptop at all; and Safari is stricter than Chrome about
self-signed roots. Budget real time, or present from the host machine.

**The camera never blocks the encounter.** Permission denied, no camera, and an insecure
context are all ordinary states with "Choose a photo instead" as the primary action. File
upload stays fully usable throughout.

---

## 4. The case worth showing

Run the **"A photographed prescription, misread — and caught"** demo case.

The paper says `AMLODIPINE 5MG`. OCR reads `AMLODIPINE SMG` — a 5 taken for an S — **at 0.94
confidence**. The engine is not hedging; it is confident and wrong, which is precisely what a
confidence threshold cannot catch.

The patient sees the crop of their own line beside the reading. The mismatch is visible rather
than remembered, and Correct is one tap. Amlodipine 5mg and 10mg are both ordinary doses, so a
misread digit there is a different prescription, not a typo.

That is the verification lane's whole argument, in one row, in ten seconds. It demonstrates the
design better than any explanation of it.

---

## 5. Handwriting will fail, and that is the honest answer

Tesseract does not read handwriting. The kiosk says so plainly, does not guess, tells the
patient the doctor will still see the photograph, and offers to let them type the important
parts. Do not apologise for this in the room — guessing at a medicine name is the single most
dangerous thing this product could do, and declining to is the feature.

---

## 6. Local development runs on one fixed port

`http://localhost:5173` — the port `vite.config.ts` already declares, nothing configurable
per-run. Past sessions accumulated stale `vite` processes on 5174/5175 serving pre-hero code
next to the current one on a different port, which is how a demo ends up presenting the wrong
build by accident. Before presenting locally:

```bash
lsof -nP -iTCP -sTCP:LISTEN | grep -E ':(517[0-9]) '   # find anything already listening
kill <pid> ...                                          # stop stale servers
cd frontend && npx vite --port 5173 --strictPort         # the one true instance
```

`--strictPort` refuses to silently move to 5174 if 5173 is taken — that failure is the point;
it means a stale process is still up and needs killing first, not working around.
