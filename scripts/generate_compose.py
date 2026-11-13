#!/usr/bin/env python3
"""Generate docker-compose.override.yml from application manifests.

Each cloned application repo (repos/<name>/) may contain a declarative
`homeserver.env` manifest (plain KEY=VALUE, parsed with an allowlist, never
sourced). Deployment values and secrets live only on the server, in
env/<name>.env (outside git).

The manifest is looked up first in the application repo
(repos/<name>/homeserver.env), then in the server-side fallback
(manifests/<name>.homeserver.env, committed in THIS repo). The application
repo manifest always wins.

This script does not know about databases. An application that needs one
lists DATABASE_URL in REQUIRED_ENV and sets it explicitly in env/<name>.env,
even when it points at the optional local Postgres (see docker-compose.yml).

Output:
  docker-compose.override.yml  - application services (generated, gitignored)
  .deploy/apps.tsv             - metadata for deploy.sh (service, health, ...)

Usage: python3 scripts/generate_compose.py [--root DIR]
"""
import argparse
import json
import re
import sys
from pathlib import Path

MANIFEST = "homeserver.env"
ALLOWED_KEYS = {
    "SERVICE", "DOCKERFILE", "CONTEXT", "PORT",
    "PUBLIC", "HEALTHCHECK", "REQUIRED_ENV",
}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _is_safe_json_string_array(value):
    """True only for a JSON array of strings, e.g. '["https://a.com"]'.

    Needed by apps that require JSON (ALLOWED_ORIGINS). Spaces are allowed
    only inside single quotes; shell metachars inside items stay forbidden.
    """
    quoted = len(value) >= 2 and value[0] == "'" and value[-1] == "'"
    if (" " in value or "\t" in value) and not quoted:
        return False
    try:
        parsed = json.loads(value[1:-1] if quoted else value)
    except ValueError:
        return False
    return isinstance(parsed, list) and all(
        isinstance(item, str)
        and not any(c in item for c in (" ", "\t", '"', "'", "`", "$", ";"))
        for item in parsed
    )


def parse_env_file(path):
    """Parse KEY=VALUE. Return (dict, error list). Never executes code."""
    values, errors = {}, []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"{path}:{lineno}: missing '=' in line: {raw!r}")
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not ENV_KEY_RE.match(key):
            errors.append(f"{path}:{lineno}: invalid variable name: {key!r}")
            continue
        if not _is_safe_json_string_array(value) and any(
            c in value for c in (" ", "\t", '"', "'", "`", "$", ";")
        ):
            errors.append(f"{path}:{lineno}: forbidden character in value of {key}")
            continue
        values[key] = value
    return values, errors


def load_manifest(repo_dir, manifests_dir=None):
    path = repo_dir / MANIFEST
    if not path.is_file() and manifests_dir is not None:
        fallback = manifests_dir / f"{repo_dir.name}.homeserver.env"
        if fallback.is_file():
            path = fallback
    if not path.is_file():
        return None, [f"{repo_dir.name}: no {MANIFEST} manifest in app repo"
                      f" and no fallback manifests/{repo_dir.name}.homeserver.env"]
    values, errors = parse_env_file(path)
    unknown = sorted(set(values) - ALLOWED_KEYS)
    if unknown:
        errors.append(f"{path}: unknown manifest fields: {', '.join(unknown)}")
    for required in ("SERVICE", "DOCKERFILE", "PORT"):
        if not values.get(required):
            errors.append(f"{path}: missing required field {required}")
    service = values.get("SERVICE", "")
    if service and not NAME_RE.match(service):
        errors.append(f"{path}: SERVICE has invalid name: {service!r}")
    port = values.get("PORT", "")
    if port and not (port.isdigit() and 1 <= int(port) <= 65535):
        errors.append(f"{path}: PORT must be a number 1-65535, got: {port!r}")
    for rel in ("DOCKERFILE", "CONTEXT"):
        v = values.get(rel, "")
        if v and (v.startswith("/") or ".." in Path(v).parts):
            errors.append(f"{path}: {rel} must be a relative path without '..': {v!r}")
    for flag in ("PUBLIC", "HEALTHCHECK"):
        if values.get(flag, "false") not in ("true", "false"):
            errors.append(f"{path}: {flag} must be true/false")
    required_env = [k for k in values.get("REQUIRED_ENV", "").split(",") if k]
    for key in required_env:
        if not ENV_KEY_RE.match(key):
            errors.append(f"{path}: REQUIRED_ENV contains invalid name: {key!r}")
    return values, errors


def ensure_server_env(env_dir, repo_name, manifest):
    """Create env/<name>.env template on the server, if missing."""
    path = env_dir / f"{repo_name}.env"
    if path.is_file():
        return path, False
    env_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Deployment values for {repo_name} - server only, outside git.",
        "# PUBLIC_HOSTNAME: public domain (no https://). Empty = private app.",
        "PUBLIC_HOSTNAME=",
        "# Extra CORS origins (comma separated), e.g. https://front.example.com",
        "CORS_EXTRA_ORIGINS=",
    ]
    for key in [k for k in manifest.get("REQUIRED_ENV", "").split(",") if k]:
        lines.append(f"{key}=")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, True


def build_app(repo_dir, env_dir, manifests_dir=None):
    """Return (app dict | None, error list, template-created flag)."""
    manifest, errors = load_manifest(repo_dir, manifests_dir)
    if manifest is None:
        return None, errors, False
    env_path, created = ensure_server_env(env_dir, repo_dir.name, manifest)
    if created:
        errors.append(
            f"Created template {env_path} - fill it in on the server and run deploy again."
        )
        return None, errors, True
    server_env, env_errors = parse_env_file(env_path)
    errors.extend(env_errors)

    required = [k for k in manifest.get("REQUIRED_ENV", "").split(",") if k]
    for key in required:
        if not server_env.get(key):
            errors.append(f"{env_path}: required variable {key} is empty")

    public = manifest.get("PUBLIC", "false") == "true"
    hostname = server_env.get("PUBLIC_HOSTNAME", "")
    if public and not hostname:
        errors.append(f"{env_path}: PUBLIC=true in manifest, but PUBLIC_HOSTNAME is empty")
    if hostname and not re.match(r"^[a-z0-9.-]+$", hostname):
        errors.append(f"{env_path}: PUBLIC_HOSTNAME looks invalid: {hostname!r}")

    if errors:
        return None, errors, created

    web_url = f"https://{hostname}" if hostname else ""
    cors = server_env.get("CORS_ORIGINS", "")
    if not cors and hostname:
        origins = [web_url]
        origins += [o for o in server_env.get("CORS_EXTRA_ORIGINS", "").split(",") if o]
        cors = ",".join(origins)

    app = {
        "repo": repo_dir.name,
        "service": manifest["SERVICE"],
        "dockerfile": manifest["DOCKERFILE"],
        "context": manifest.get("CONTEXT", "."),
        "port": manifest["PORT"],
        "healthcheck": manifest.get("HEALTHCHECK", "false") == "true",
        "hostname": hostname,
        "cf_zone_id": server_env.get("CF_ZONE_ID", ""),
        "web_url": web_url,
        "cors_origins": cors,
        "env_file": env_path,
    }
    return app, [], created


def render_compose(apps):
    lines = [
        "# GENERATED by scripts/generate_compose.py - do not edit by hand.",
        "# Source: homeserver.env manifests in repos/*/ and env/*.env on server.",
        "services:",
    ]
    for app in apps:
        q = lambda v: json.dumps(str(v))
        context = f"./repos/{app['repo']}"
        if app["context"] != ".":
            context += "/" + app["context"]
        lines += [
            f"  {app['service']}:",
            "    build:",
            f"      context: {context}",
            f"      dockerfile: {q(app['dockerfile'])}",
        ]
        lines += [
            "    restart: unless-stopped",
            # YAML anchors do not work across compose files; inline logging.
            "    logging:",
            "      driver: json-file",
            "      options:",
            '        max-size: "10m"',
            '        max-file: "3"',
            "    env_file:",
            f"      - env/{app['repo']}.env",
            "    environment:",
            "      NODE_ENV: production",
        ]
        if app["web_url"]:
            lines.append(f"      WEB_URL: {q(app['web_url'])}")
        if app["cors_origins"]:
            lines.append(f"      CORS_ORIGINS: {q(app['cors_origins'])}")
        lines += ["    networks:", "      - internal", ""]
    return "\n".join(lines)


def render_tsv(apps):
    rows = ["repo\tservice\tport\thealthcheck\thostname\tcf_zone_id"]
    for a in apps:
        rows.append("\t".join([
            a["repo"], a["service"], a["port"],
            "true" if a["healthcheck"] else "false", a["hostname"], a["cf_zone_id"],
        ]))
    return "\n".join(rows) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent.parent))
    args = parser.parse_args(argv)
    root = Path(args.root)
    repos_dir, env_dir = root / "repos", root / "env"
    manifests_dir = root / "manifests"

    repo_dirs = sorted(p for p in repos_dir.glob("*") if p.is_dir()) if repos_dir.is_dir() else []
    if not repo_dirs:
        print("No cloned repositories in repos/ - deploy fetches code first.", file=sys.stderr)
        return 1

    apps, all_errors = [], []
    services = {}
    for repo_dir in repo_dirs:
        app, errors, _ = build_app(repo_dir, env_dir, manifests_dir)
        all_errors.extend(errors)
        if app:
            if app["service"] in services:
                all_errors.append(
                    f"Duplicate SERVICE={app['service']}: {services[app['service']]} and {repo_dir.name}"
                )
            services[app["service"]] = repo_dir.name
            apps.append(app)

    if all_errors:
        print("Cannot generate application configuration:", file=sys.stderr)
        for e in all_errors:
            print(f"  - {e}", file=sys.stderr)
        return 1

    (root / "docker-compose.override.yml").write_text(render_compose(apps), encoding="utf-8")
    deploy_dir = root / ".deploy"
    deploy_dir.mkdir(exist_ok=True)
    (deploy_dir / "apps.tsv").write_text(render_tsv(apps), encoding="utf-8")
    for a in apps:
        route = f" -> https://{a['hostname']}" if a["hostname"] else " (private)"
        print(f"  {a['service']} (repos/{a['repo']}, port {a['port']}){route}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
