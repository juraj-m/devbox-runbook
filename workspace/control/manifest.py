"""projects.toml, as ws and the control plane read it.

One thing on top of plain TOML: `like = "OTHER"` makes a project a copy of
another, with its own keys on top. For a second checkout of the same repo
(shop-b: shop's Stack B, in ~/_/shop-b). Two keys are never
copied, because a copy must not share them: `dir` (it gets a folder of its
own, by its name) and `tailnet` (the same ports would collide; a copy serves
none until it names its own). Optional parts are copied one by one: a copy
names only those it changes. One level only: a copy of a copy is refused.
"""
import tomllib

NOT_COPIED = ("dir", "tailnet")


class ManifestError(ValueError):
    pass


def load(path):
    with open(path, "rb") as f:
        return resolve(tomllib.load(f))


def resolve(projects):
    out = {}
    for name, p in projects.items():
        base = p.get("like")
        if base is None:
            out[name] = p
            continue
        if base not in projects:
            raise ManifestError(f"{name}: like = {base!r}: no such project")
        if "like" in projects[base]:
            raise ManifestError(f"{name}: like = {base!r}, which is a copy itself (one level only)")
        merged = {k: v for k, v in projects[base].items() if k not in NOT_COPIED}
        merged.update({k: v for k, v in p.items() if k not in ("like", "optional")})
        # Optional parts one by one: a copy names only those it changes.
        merged["optional"] = {**projects[base].get("optional", {}), **p.get("optional", {})}
        out[name] = merged
    return out
