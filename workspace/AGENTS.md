# The workspace, for agents

Projects live under `~/_`. `ws` (on PATH, from dev-server `workspace/`) knows
each one: how it's built, started, stopped and checked (`projects.toml`).
`ws list` names them; `ws plan NAME` shows what a project needs.

## Long or heavy commands: `ws run`

Run production builds, full test suites, and anything that takes more than
a couple of minutes or a gigabyte of memory as a job, not in your own
shell:

```sh
ws run shop build-admin --wait -- 'cd shop-app && npx ng build admin --configuration=production'
```

- It runs apart from you: if your command times out or your session ends,
  the job keeps going. `--wait` streams its output and exits with its code;
  without it, `ws run` returns at once.
- One job at a time per project, the rest queued: each Angular production
  build takes ~3 GB and every core, so builds in parallel are no faster
  and can starve the machine. `--parallel` only for small jobs.
- `ws jobs NAME` (states, durations, exit codes), `ws logs NAME JOB [-f]`,
  `ws cancel NAME JOB`.
- A project can name its jobs in `projects.toml` (`[NAME.jobs]`); then
  `ws run NAME JOB --wait` is enough.
- On the coding box a job is capped at 6 GB. "failed (signal 9): over the
  memory cap" means it needed more: say so, don't retry it in parallel.

## Servers

`ws start NAME` / `ws stop NAME`, not the dev servers by hand: on the box
that keeps each stack's processes together (measured, stopped as a whole).
`ws check NAME` is the project's own health check.

On the box a stack can be paused (`ws pause`, `ws resume`, `ws switch`):
frozen, its memory in swap. A paused stack's ports take connections and
never answer, so a request to it hangs: if one does, `ws top` says whether
it's paused. Don't resume or pause a stack you didn't start without asking.

## Claude sessions on the box

`ws claude NAME` starts another Claude session in a stack (Remote Control,
in the stack's tmux); `ws claudes` lists them with what each is doing.
Don't end or answer one you didn't start.

## The coding box

Its memory is shared by every stack and every session; `ws top` shows what
uses it, by stack (the same as the control page, https://devbox.<tailnet>.ts.net:7070).
Before starting something big, look: `ws peaks` says how much of what's
free the running stacks, sessions and jobs could take at their peaks, and
what a job like yours has taken before. Both also show the disk: under
20 GB free they say so first, and a big build or install may not fit.
Stop what you started when you're done with it.

### Its internet is an allowlist

The box reaches only listed hosts (`devbox-egress status` shows the mode).
A host that isn't listed looks like one that doesn't exist: "Could not
resolve host", `ENOTFOUND`, `getaddrinfo` / `NXDOMAIN`, or a connection refused
or reset on an address you used directly. When a download, install, API
call or test fails like that:

1. `devbox-egress blocked` lists the names asked for that aren't listed;
   find the one your command needed.
2. If the task really needs it, run `devbox-egress request <domain> "<why>"`.
   Give the narrowest name that works, such as `registry.example.com` rather
   than `example.com`. Then ask the user (AskUserQuestion): the domain, what
   needs it, and that they approve it with
   `ssh -t root@devbox devbox-egress approve`. Carry on with other work
   while you wait.
3. Don't route around the list: no other mirror, proxy, DNS server, IP
   address or container to reach a host it leaves out. Leaving a host out
   is the point.

In audit mode nothing is blocked yet, so none of this comes up.
