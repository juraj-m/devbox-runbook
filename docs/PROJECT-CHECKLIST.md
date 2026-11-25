# What makes a project box-ready

What a project needs so that `ws up NAME` builds it on the box and you can
use it from a tablet's or phone's browser. I learned these getting two real
projects there: an Angular + Supabase + Cloudflare Workers monorepo, and a
Python + Neo4j + Angular app. Each item says what broke without it.

Construction lives in `projects.toml`; running and verifying stay in the
project ([ARCHITECTURE.md, "Who owns what"](ARCHITECTURE.md#the-workspace-ws-and-projectstoml)),
so most of these are changes in the project's own repo.

## The model

- The box runs the project's dev stack. You use it from another device's
  browser over the tailnet.
- **Every port is served over HTTPS** at
  `https://devbox.<tailnet>.ts.net:<port>` by Tailscale, to the project's
  own server on the same port number. URLs differ from local ones only in
  scheme and host.
- Why HTTPS: a page over plain http (other than `localhost`) has no secure
  context, so `navigator.locks`, `crypto.subtle`, `crypto.randomUUID`,
  service workers and secure cookies are missing. Supabase auth died at
  startup with "Cannot read properties of undefined (reading 'request')"
  (`navigator.locks.request`).

## Checklist

### Running

1. **Recipes run in bash on Linux.** Ubuntu's `/bin/sh` is dash. A
   Makefile that uses `source` must set `SHELL := /bin/bash`; one failed at
   "source: not found" and still exited 0.
2. **Long-running servers start in the background, with start, stop,
   status and logs**, not only as a foreground command: the box has no
   terminal attached. A start waits until the server answers HTTP (not just
   until its port opens) and fails loudly if it doesn't. A stop takes the
   whole process tree (killing only the port's process lets a supervisor
   like wrangler start another) and never kills a process that isn't the
   project's. Name them in `projects.toml` (`start = { api = "make
   api-restart" }`), and give `stop` commands for what isn't a process of
   the stack (its containers). A project without a Makefile can use
   `serve` instead.
3. **Servers bind `${DEV_BIND_HOST:-0.0.0.0}`.** Bootstrap sets
   `DEV_BIND_HOST=127.0.0.1` on the box. Tailscale holds each served port
   on the tailnet address, so a server that binds `0.0.0.0` after it fails
   with "port in use". (One started *before* Tailscale took the port
   happens to coexist, which hid this at first.) Elsewhere the default
   keeps LAN access. For what can't bind `127.0.0.1` (the Supabase CLI
   publishes `0.0.0.0`, no setting for it), `ws start` takes the project's
   tailnet ports down while its start commands run and serves them again
   after. Then other devices reach it, but the box itself can't through its
   tailnet name: use `127.0.0.1:<port>` on the box. Examples: wrangler
   `--ip`, `ng serve --host`, `astro dev --host`.
4. **Fits in memory** next to the other stacks. A stack with four Angular
   dev servers uses ~10 GB. Say which parts are optional
   (`optional.<x>.start`).

### URLs, origins, hosts

5. **Client URLs come from the page's own location**:
   `${location.protocol}//${location.hostname}:<port>`, never a hard-coded
   `http://localhost` or `http://` (an https page can't call http: mixed
   content). That goes for every one of them: the API, websockets (`wss:`
   on an https page), and telemetry exporters (an OTLP URL was a separate
   setting that got missed).
6. **CORS allows the box's exact origin**,
   `https://devbox.<tailnet>.ts.net` with any port, listed by name, in
   development only. Not `*.ts.net`: Tailscale Funnel serves public sites
   under it, and those must not reach your local servers (a security
   review caught that). Not plain http to the box: useless without a
   secure context.
7. **Dev servers accept the box's host name.** Angular and Vite reject
   unknown `Host` headers ("Invalid Host header", 403). Allow
   `devbox.<tailnet>.ts.net` only, and keep the check for everything else.
   For a Vite server run by `serve`, put
   `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=.ts.net` in front of the command,
   as the example `projects.toml` does.
8. **Auth redirect URLs** (Supabase `site_url`,
   `additional_redirect_urls`, OAuth apps) include the box's https origin,
   for magic links and OAuth.
9. **A page Content-Security-Policy lists the box's API origins** in its
   dev variant (`connect-src https://devbox.<tailnet>.ts.net:<port>
   wss://…:<port>`, by name). Otherwise the browser blocks the calls that
   item 5 made correct. The production CSP stays as it is.

### Building

10. **Toolchain versions are declared** (node, deno, Python, CLI tools), so
    `projects.toml` can pin them for mise. Use CI's versions where CI has
    them. A conda env can be created with micromamba (a mise tool) from the
    same `environment.yml` the laptop's conda uses.
11. **Dependencies install from committed lockfiles** with the tool CI
    uses. Installs must not run under a running stack: `npm ci` empties
    `node_modules` and kills running servers. Guard it in `prepare`: run it
    only when the lockfile is newer than `node_modules/.package-lock.json`.
12. **Generated files have a command** (env files, icon sprites, configs)
    and are produced by `prepare` or the project's start, not left over
    from an old checkout.
13. **Secrets come from sops files**, written before the dependency install
    if an install hook reads them. The box decrypts dev and test only;
    nothing may need a prod value to run.
14. **A database starts from nothing.** A container's first start sets its
    password from the secrets (Neo4j otherwise starts as neo4j/neo4j and
    refuses everything), and its healthcheck allows for a first start's
    extra time. Dev data comes from a seed, never a copy of prod. A seed
    that wipes first runs only on an empty database unless forced.

### Verifying

15. **A health check in the project** that names what is down and why,
    run by `ws check`.
