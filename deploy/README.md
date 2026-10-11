# Putting the trackers on phones

One command starts everything on your Mac, one port is shared with the internet, and one address goes in Vercel.

    ./jh up        # starts every profile's tracker AND the gateway (the single public port, 8780)
    ./jh down      # stops all of it
    ./jh ps        # what is running

    phone -> Vercel address (optional) -> Cloudflare Tunnel -> gateway :8780 -> the signed-in person's tracker

The **gateway** shows the sign-in page, checks the email and password, and sends each person to the tracker of the
profile they belong to (yours to :8787, Parnika's to :8788, and so on). The trackers themselves only listen on your
Mac. Cloudflare needs to know about port **8780 only**, however many people you add.

## 1. Users (who can sign in)

Users live in one small table (`auth.db`), each tied to one profile. Passwords are stored only as hashes.

    ./jh users                                             # list
    ./jh users add her@email.com --profile english-hons    # add (asks for a password) or change a password
    ./jh users passwd her@email.com
    ./jh users remove her@email.com

Changes apply immediately. Use strong passwords for anything reachable from the internet: five wrong guesses on one
address, or on one email, lock sign-in for 15 minutes.

## 2. Cloudflare Tunnel to port 8780

A **named tunnel** keeps the address forever (free Cloudflare account, a domain whose DNS is on Cloudflare):

    brew install cloudflared
    cloudflared tunnel login
    cloudflared tunnel create jobhunter
    cloudflared tunnel route dns jobhunter jobs.YOURDOMAIN.com

`~/.cloudflared/config.yml` (use the id that `create` printed):

    tunnel: jobhunter
    credentials-file: /Users/krishwadhwa/.cloudflared/<TUNNEL-ID>.json
    ingress:
      - hostname: jobs.YOURDOMAIN.com
        service: http://127.0.0.1:8780
      - service: http_status:404

Run it, and make it start by itself at login:

    cloudflared tunnel run jobhunter            # try it first
    sudo cloudflared service install            # then install it as a background service

Open https://jobs.YOURDOMAIN.com on a phone: you should see the sign-in page. Without a domain,
`cloudflared tunnel --url http://127.0.0.1:8780` prints a temporary address that changes on every restart.

## 3. Vercel in front (optional)

`deploy/vercel` is a tiny project (`api/proxy.js` + `vercel.json`) that forwards every request, form posts included, to
your Cloudflare address.

1. In Vercel: New Project, import the GitHub repo, set **Root Directory** to `deploy/vercel` and **Application Preset**
   to **Other**. Leave Build, Output and Install commands empty.
2. Environment Variables: `BACKEND_URL = https://jobs.YOURDOMAIN.com` (https, no trailing slash). Deploy.
3. Open the Vercel address on the phone: you should see the sign-in page. Nothing else needs configuring: the proxy
   tells the gateway which public address the browser used, so sign-in and buttons work as they are.
4. (Optional) `JOBHUNTER_PUBLIC_URL=https://YOUR-PROJECT.vercel.app` in the `.env` next to `jh`, then `./jh up`, makes
   Telegram alerts link to the phone address. If buttons ever answer "Blocked: this request did not come from the
   site itself", also add `JOBHUNTER_ALLOWED_ORIGINS=https://YOUR-PROJECT.vercel.app`.

Uploads through Vercel are limited to about 4.5 MB (a normal resume is far smaller).

### If sign-in fails

1. Open `https://jobs.YOURDOMAIN.com/healthz` (the Cloudflare address, not Vercel). It must show `{"ok":true,"gateway":true}`.
   Without `"gateway":true` the tunnel points at a profile's port instead of **8780**.
2. Try signing in on the Cloudflare address directly. If that works but Vercel does not, the problem is in the Vercel project.
3. `tail -20 .gateway/gateway.log` on the Mac shows every attempt: `login FAILED for kr***@gmail.com ... (wrong password)`,
   `(empty)` when the form fields did not arrive, or `blocked a cross-site POST` with the addresses it compared.

## 4. Add it to her home screen

Safari (iPhone): Share, Add to Home Screen. Chrome (Android): menu, Install app. It then opens full screen.

## 5. Keep the Mac available

The site is only up while the Mac is awake, online and `./jh up` has run.

* System Settings, Battery (or Energy): prevent automatic sleeping when the display is off (on the power adapter).
* Start everything at login:

      cp deploy/mac/com.jobhunter.plist ~/Library/LaunchAgents/
      launchctl load ~/Library/LaunchAgents/com.jobhunter.plist

  After that `./jh up` is only needed when you change code or settings.
* If the Mac is off, people see "This tracker is not running" after signing in.

## Adding another person later

    ./jh new anna english "Anna"
    ./jh users add anna@email.com --profile anna
    ./jh up

Nothing else changes: no new port, no new tunnel entry, no Vercel change.

## What she can do from the phone

Browse and search jobs, change roles and settings, upload her resume, hide jobs, mark "Already applied", find
referrers, add or pause companies, and press **Apply**: on the phone that prepares a resume tailored to the job (PDF
or Word) and shows "Open posting and apply". She submits the application on the company's own site. The Chrome
auto-fill is only available on the Mac itself.

## Safety notes

* Only the gateway (127.0.0.1:8780) is meant to be reached from outside; the trackers and their passwords-less local
  access stay on the Mac. The gateway and the trackers share a secret in `.gateway/secret` so a tracker can tell a
  request really came through the gateway. Keep `auth.db` and `.gateway/` private.
* Sessions last 30 days and end when the password changes, the user is removed, or she presses Sign out.
* Visitors never see other profiles, and cannot make the server fetch private addresses.
