"""Generate `THIRD-PARTY-LICENSES` from the lockfiles, not from memory.

**Why this is generated rather than written.** The attribution document has to list what the project
actually resolves, and a hand-maintained list is wrong the first time a lockfile moves. This reads
the five `uv.lock` files and the three `package-lock.json` trees, so regenerating after a dependency
bump is one command and a diff.

**Where the licenses come from, and the honest limitation.** `uv.lock` records names, versions and
hashes but no license, so the Python licenses are read from the installed distribution metadata in
each package's `.venv` (`License-Expression`, then `License`, then the `License ::` classifiers).
That means a package that is locked but not installed locally resolves to `UNKNOWN` rather than
silently to nothing, and the report says so at the end. npm lockfiles do carry a `license` field, so
those are read directly.

Run from the repository root:

    python3 scripts/generate_third_party_licenses.py

Anything reported as `UNKNOWN` needs resolving by hand before the attribution document is complete.
"""

from __future__ import annotations

import json
import pathlib
import re
import tomllib
from collections import defaultdict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Each Python component: its lockfile, and the venv whose installed metadata carries the licenses.
PYTHON_COMPONENTS = {
    "backend": "backend",
    "tools": "tools",
    "evaluation": "evaluation",
    "conversation-api": "conversation-api",
    "agent": "agent/MultiTenantTravel/app/MultiTenantTravel",
}

NPM_COMPONENTS = {
    "frontend": "frontend/package-lock.json",
    "infra": "infra/package-lock.json",
    "agent-cdk": "agent/MultiTenantTravel/agentcore/cdk/package-lock.json",
}

UNKNOWN = "UNKNOWN"

# **Packages whose license cannot be read locally, resolved from upstream metadata by hand.**
# Each entry needs a reason, because an override is an assertion this script cannot re-derive: if it
# is wrong, nothing here will catch it. The first two are Windows-only conditional dependencies, so
# they are locked on every platform and installed on none of ours.
OVERRIDES = {
    # PyPI classifier "License :: OSI Approved :: BSD License"; its own README states BSD 3-Clause.
    ("python", "colorama"): "BSD-3-Clause",
    # PyPI `license` field "PSF", classifier "OSI Approved :: Python Software Foundation License".
    ("python", "pywin32"): "PSF-2.0",
    # Its `License:` field reads "Dual License", naming no terms. The classifiers say which two:
    # "OSI Approved :: BSD License" and "OSI Approved :: Apache Software License". Stated as the
    # disjunction it is, so the attribution document does not leave a reader guessing.
    ("python", "python-dateutil"): "Apache-2.0 OR BSD-3-Clause",
}


def _license_from_metadata(text: str) -> str | None:
    """The license from one installed distribution's `METADATA`.

    Order matters: `License-Expression` is the modern SPDX field and is authoritative when present.
    A bare `License:` field is next. The `License ::` classifiers are the fallback, and they are
    joined rather than picked from, because a package declaring two is dual-licensed and collapsing
    that to one would misstate it.
    """
    if match := re.search(r"^License-Expression:\s*(.+)$", text, re.MULTILINE):
        return match.group(1).strip()
    if match := re.search(r"^License:\s*(.+)$", text, re.MULTILINE):
        value = match.group(1).strip()
        # Some packages paste an entire license text into the field. A newline-free short value is a
        # name; anything longer is a body, and the classifiers describe it better.
        if value and len(value) < 60:
            return value
    classifiers = re.findall(r"^Classifier:\s*License\s*::\s*(.+)$", text, re.MULTILINE)
    if classifiers:
        names = [c.split("::")[-1].strip() for c in classifiers]
        return " OR ".join(dict.fromkeys(names))
    return None


def _installed_licenses(venv: pathlib.Path) -> dict[str, str]:
    """Map of normalized distribution name to license, from one venv's `dist-info` directories."""
    found: dict[str, str] = {}
    for metadata in venv.glob("lib/python*/site-packages/*.dist-info/METADATA"):
        try:
            text = metadata.read_text(errors="replace")
        except OSError:
            continue
        name = re.search(r"^Name:\s*(.+)$", text, re.MULTILINE)
        if not name:
            continue
        if license_name := _license_from_metadata(text):
            found[_normalize(name.group(1).strip())] = license_name
    return found


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def python_packages() -> dict[str, list[tuple[str, str, str]]]:
    """Locked third-party Python packages per component, as `(name, version, license)`."""
    per_component: dict[str, list[tuple[str, str, str]]] = {}
    for component, path in PYTHON_COMPONENTS.items():
        root = REPO_ROOT / path
        lock = tomllib.loads((root / "uv.lock").read_text())
        installed = _installed_licenses(root / ".venv")
        rows: list[tuple[str, str, str]] = []
        for package in lock.get("package", []):
            source = package.get("source") or {}
            # The project itself and any path dependency are ours, not third-party.
            if "editable" in source or "virtual" in source:
                continue
            name = package["name"]
            # The override wins over the installed value: some packages declare something that
            # names no terms ("Dual License"), and an override exists precisely to replace it.
            license_name = OVERRIDES.get(("python", name)) or installed.get(
                _normalize(name), UNKNOWN
            )
            rows.append((name, package.get("version", ""), license_name))
        per_component[component] = sorted(rows)
    return per_component


def _npm_license(entry: dict) -> str | None:
    """The license from one lockfile entry or manifest, in any of the shapes npm has used.

    Three shapes, because npm's field has changed twice and old packages still use the old ones:
    `license: "MIT"`, the deprecated `license: {type, url}`, and the older plural
    `licenses: [{type, url}]`. Missing the plural is how `exit` resolved to UNKNOWN while declaring
    MIT in its own manifest.
    """
    for key in ("license", "licenses"):
        value = entry.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            if found := value.get("type"):
                return found
        if isinstance(value, list) and value:
            types = [
                item.get("type") for item in value if isinstance(item, dict) and item.get("type")
            ]
            if types:
                return " OR ".join(dict.fromkeys(types))
    return None


def _npm_license_from_disk(package_dir: pathlib.Path) -> str | None:
    """The license from an installed package's own `package.json`.

    **Needed because some lockfile entries carry no license at all.** `exit` is the example: its
    `package.json` uses the deprecated `licenses` array, which npm does not copy into the lockfile,
    so the lockfile says nothing while the installed package says MIT.
    """
    manifest = package_dir / "package.json"
    if not manifest.is_file():
        return None
    try:
        return _npm_license(json.loads(manifest.read_text()))
    except (OSError, json.JSONDecodeError):
        return None


def npm_packages() -> dict[str, list[tuple[str, str, str]]]:
    """Locked npm packages per component, as `(name, version, license)`."""
    per_component: dict[str, list[tuple[str, str, str]]] = {}
    for component, path in NPM_COMPONENTS.items():
        lock_path = REPO_ROOT / path
        lock = json.loads(lock_path.read_text())
        rows: list[tuple[str, str, str]] = []
        for location, entry in (lock.get("packages") or {}).items():
            # The root entry is the project itself; a `link` is a workspace pointer, not a download.
            if not location or entry.get("link"):
                continue
            name = entry.get("name") or location.split("node_modules/")[-1]
            license_name = _npm_license(entry) or _npm_license_from_disk(
                lock_path.parent / location
            )
            rows.append((name, entry.get("version", ""), license_name or UNKNOWN))
        per_component[component] = sorted(set(rows))
    return per_component


def render(python: dict[str, list], npm: dict[str, list]) -> str:
    lines = [
        "# Third-Party Licenses",
        "",
        "This project depends on the third-party packages listed below. Each is used under the",
        "license shown, which is the license the package itself declares.",
        "",
        "Generated by `scripts/generate_third_party_licenses.py` from the committed lockfiles.",
        "Regenerate after any dependency change rather than editing this file by hand.",
        "",
    ]

    totals: dict[str, int] = defaultdict(int)
    for group, components in (("Python", python), ("Node.js", npm)):
        lines.append(f"## {group}")
        lines.append("")
        for component, rows in components.items():
            lines.append(f"### {component}")
            lines.append("")
            lines.append("| Package | Version | License |")
            lines.append("| --- | --- | --- |")
            for name, version, license_name in rows:
                lines.append(f"| `{name}` | {version} | {license_name} |")
                totals[license_name] += 1
            lines.append("")

    lines.append("## Licenses in use")
    lines.append("")
    lines.append("| License | Packages |")
    lines.append("| --- | --- |")
    for license_name, count in sorted(totals.items(), key=lambda item: (-item[1], item[0])):
        lines.append(f"| {license_name} | {count} |")
    lines.append("")

    # **Surfaced rather than buried.** Nobody reads 755 rows, so the licenses that are not plainly
    # permissive are listed again here. This is a prompt for a human decision, not a verdict: the
    # release process asks whether any dependency license needs a policy exception, and these are
    # the ones that question is about.
    notable = [
        (name, version, license_name)
        for components in (python, npm)
        for rows in components.values()
        for name, version, license_name in rows
        if _needs_attention(license_name)
    ]
    if notable:
        lines.append("## Licenses to review before release")
        lines.append("")
        lines.append("Not blockers, and not a legal opinion. These are the entries that are either")
        lines.append("copyleft, dual-licensed, ambiguous as declared, or not a software license at")
        lines.append("all, so they are the ones worth a deliberate look.")
        lines.append("")
        lines.append("| Package | Version | License | Why it is listed |")
        lines.append("| --- | --- | --- | --- |")
        for name, version, license_name in sorted(set(notable)):
            lines.append(
                f"| `{name}` | {version} | {license_name} | {_attention_reason(license_name)} |"
            )
        lines.append("")
    return "\n".join(lines)


AMBIGUOUS = {"dual license", "unknown"}


def _needs_attention(license_name: str) -> bool:
    lowered = license_name.lower()
    if lowered in AMBIGUOUS:
        return True
    return any(
        marker in lowered for marker in ("mpl", "gpl", "cc-by", "cc0", "epl", "cddl", "osl", "eupl")
    )


def _attention_reason(license_name: str) -> str:
    lowered = license_name.lower()
    if lowered in AMBIGUOUS:
        return "declared license is ambiguous; confirm the actual terms upstream"
    if "gpl" in lowered:
        return "copyleft option present; confirm the permissive option is the one relied on"
    if "mpl" in lowered or "epl" in lowered or "cddl" in lowered or "osl" in lowered:
        return "weak copyleft, file-level; usually fine unmodified, worth confirming"
    if "eupl" in lowered:
        return "copyleft; confirm compatibility"
    if "cc-by" in lowered or "cc0" in lowered:
        return "content license rather than a software license; attribution may apply"
    return "review"


def main() -> int:
    python = python_packages()
    npm = npm_packages()
    output = REPO_ROOT / "THIRD-PARTY-LICENSES"
    output.write_text(render(python, npm))

    unknown = [
        f"{component}: {name} {version}"
        for components in (python, npm)
        for component, rows in components.items()
        for name, version, license_name in rows
        if license_name == UNKNOWN
    ]
    counted = sum(len(rows) for components in (python, npm) for rows in components.values())
    print(f"wrote {output.relative_to(REPO_ROOT)}: {counted} package entries")
    if unknown:
        print(f"\n{len(unknown)} package(s) with no license found; resolve these by hand:")
        for item in unknown:
            print(f"  - {item}")
        return 1
    print("every package resolved to a declared license")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
