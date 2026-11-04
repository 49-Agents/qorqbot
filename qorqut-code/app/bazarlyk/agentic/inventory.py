"""Inventory explicit offline v1.3 asset roots without importing their code."""

from __future__ import annotations

import ast
from copy import deepcopy
import posixpath
import re
from urllib.parse import unquote, urlsplit

from .support import ImportFailure, Source, absolute, decoded, digest, encoded, identifier, read_yaml, relative


SOURCE_IDS = {"host", "roles", "email", "content", "content-library", "paper", "fastmail", "telegram", "whatsapp"}
ROLE_IDS = ("ordubashy", "global-worker", "learning-applier")
CONTENT_SKILLS = ("content-creation", "content-foundations", "content-gallery",
                  "content-demand-thinking", "pull-framework", "pull-framework-content")
HOST_PYTHON = ("agents/role_workflow.py", "agents/cleanup_workflow.py", "agents/context.py", "agents/engineering.py", "inbox/defaults.py", "bootstrap/email_guidance.py")


def package_area(package_id):
    if package_id == "imported-roles":
        return "roles"
    if package_id == "overseer-role-skills":
        return "overseer"
    if package_id in {"imported-role-skills", "imported-host-builtins", "imported-host-builtins-templates", "runtime-instructions"}:
        return "operations"
    return "plugins"


def package_path(package_id):
    identifier(package_id)
    return "user-preferences" if package_id == "user-preferences" else f"{package_area(package_id)}/{package_id}"


class Inventory:
    def __init__(self):
        self.sources, self.packages, self.bindings, self.diagnostics, self.assets = {}, {}, [], [], []
        self.native_ids = {}
        self.declarations, self.composition = {}, {}

    def attempt(self, source, function, *args, **kwargs):
        try:
            return function(*args, **kwargs)
        except ImportFailure as exc:
            source.issue(exc.code, exc.message, **exc.details)
            return None
        except (UnicodeError, SyntaxError) as exc:
            source.issue("invalid_source", "Source declaration is not valid UTF-8/Python", reason=str(exc))
            return None

    def package(self, package_id, owner, enabled):
        if package_id not in self.packages:
            self.packages[package_id] = dict(id=package_id, path=package_path(package_id), name=package_id,
                description=f"Offline native assets for {owner}; metadata is not authority.",
                native={"owner": owner, "enabled": enabled, "offline_only": True}, entries=[], files={})
        return self.packages[package_id]

    def put(self, source, package, path, *, kind="resource", template=False, destination=None, data=None,
            symbol=None, line=None, status="preserved"):
        body = source.read(path) if data is None else data
        destination = destination or "native/" + path
        relative(destination)
        sha = digest(body)
        previous = package["files"].get(destination)
        if previous and previous["sha256"] != sha:
            raise ImportFailure("duplicate_destination", "Different assets have the same destination", path=path)
        provenance = dict(source_id=source.id, path=path, commit=source.commit,
                          source_sha256=source.used.get(path, sha), symbol=symbol, line=line)
        row = dict(**provenance, sha256=sha, destination=f"{package['path']}/{destination}",
                   package_id=package["id"], kind=kind, native_owner=source.owner,
                   template=template, enabled=package["native"]["enabled"] and not template, status=status)
        existing_row = next((item for item in self.assets if item["destination"] == row["destination"]
                             and item["source_id"] == source.id and item["path"] == path), None)
        if existing_row is None:
            self.assets.append(row)
        elif kind != "resource":
            existing_row.update(row)
        package["files"][destination] = dict(content_base64=encoded(body), sha256=sha, provenance=provenance)
        return destination

    def entry(self, source, package, path, kind="skill", *, template=False, entry_id=None, **kwargs):
        destination = self.put(source, package, path, kind=kind, template=template, **kwargs)
        entry_id = entry_id or "asset-" + digest(destination.encode())[:20]
        found = next((entry for entry in package["entries"] if entry["path"] == destination), None)
        if found:
            return f"{package['id']}/{found['id']}"
        description = f"Native {kind}: {path}"
        if kind == "skill" and not template:
            try:
                text = decoded(package["files"][destination]["content_base64"]).decode("utf-8", "strict")
            except UnicodeError as exc:
                raise ImportFailure("invalid_skill", "SKILL.md must be UTF-8", path=path) from exc
            if text.startswith("---\n"):
                parts = text.split("\n---", 2)
                if len(parts) < 2:
                    raise ImportFailure("invalid_frontmatter", "Unclosed SKILL.md frontmatter", path=path)
                frontmatter = read_yaml(parts[0][4:].encode())
                if isinstance(frontmatter.get("description"), str) and frontmatter["description"].strip():
                    description = frontmatter["description"]
        package["entries"].append(dict(id=identifier(entry_id), kind="template" if template else kind,
            path=destination, description=description, when_to_use=description,
            native={"source_id": source.id, "owner": source.owner, "template": template,
                    "enabled": package["native"]["enabled"] and not template}))
        return f"{package['id']}/{entry_id}"

    def tree(self, source, package, root, *, template=False, destination_prefix="native/", advertise=True):
        refs = {}
        for path in source.tree(root, root_resources=not root):
            target = destination_prefix + path
            if posixpath.basename(path) == "SKILL.md" and advertise:
                refs[path] = self.entry(source, package, path, template=template, destination=target)
            else:
                self.put(source, package, path, template=template, destination=target)
        return refs

    def binding(self, source, native_id, package, refs, *, kind, template=False, **extra):
        key = (kind, native_id)
        identity = (source.id, package["id"])
        if key in self.native_ids and self.native_ids[key] != identity:
            source.issue("duplicate_native_id", "Native ID has incompatible owners", native_id=native_id)
        self.native_ids[key] = identity
        self.bindings.append(dict(kind=kind, native_id=native_id, source_id=source.id,
            owner=source.owner, package_id=package["id"], entry_refs=sorted(set(refs)),
            enabled=package["native"]["enabled"] and not template, template=template, **extra))

    def native_manifest(self, source, package, path, *, base="", template=False, role=False, canonical=None):
        body = source.read(path)
        manifest = read_yaml(body)
        native_id = identifier(manifest.get("id"))
        self.put(source, package, path, data=body, kind="manifest", template=template)
        refs, skill_bindings = [], []
        resolve = lambda value: relative(posixpath.join(base, relative(value)))
        if role:
            brief = resolve(manifest.get("brief"))
            refs.append(self.entry(source, package, brief, "instruction"))
        else:
            for field in ("tools", "rules", "config_schema"):
                value = resolve(manifest.get(field))
                self.put(source, package, value, kind="metadata", template=template)
        skills = manifest.get("skills", [])
        if not isinstance(skills, list):
            raise ImportFailure("invalid_manifest", "skills must be a list", path=path)
        seen = set()
        for skill in skills:
            if not isinstance(skill, dict):
                raise ImportFailure("invalid_manifest", "skill must be a mapping", path=path)
            skill_id, skill_path = identifier(skill.get("id")), resolve(skill.get("path"))
            if skill_id in seen:
                raise ImportFailure("duplicate_native_id", "Duplicate skill ID within native manifest", path=path)
            seen.add(skill_id)
            if posixpath.basename(skill_path) != "SKILL.md":
                raise ImportFailure("invalid_manifest", "Native skill path must end in SKILL.md", path=skill_path)
            if canonical is not None:
                target_path = f"skills/{skill_id}/SKILL.md"
                target = canonical["files"].get("native/" + target_path)
                if target is None:
                    raise ImportFailure("missing_canonical_skill", "Native content skill has no adapted entry", path=skill_path)
                source_bytes = source.read(skill_path)
                ref = next(f"{canonical['id']}/{e['id']}" for e in canonical["entries"] if e["path"] == "native/" + target_path)
                # Original native bytes remain reconstructable, but never enter the active catalog as a second skill.
                self.tree(source, package, posixpath.dirname(skill_path), advertise=False,
                          destination_prefix="native/provenance-snapshot/" )
                choice = dict(native_skill_id=skill_id, canonical_entry=ref,
                    original_sha256=digest(source_bytes), canonical_sha256=target["sha256"],
                    adaptation="qorqut-content/PROVENANCE.md", snapshot_only=True,
                    provenance_snapshot="native/provenance-snapshot/" + skill_path)
            else:
                found = self.tree(source, package, posixpath.dirname(skill_path), template=template)
                ref = found[skill_path]
                choice = dict(native_skill_id=skill_id, canonical_entry=ref)
            refs.append(ref)
            skill_bindings.append(choice)
        self.binding(source, native_id, package, refs, kind="role" if role else "plugin", template=template,
                     manifest_path=path, skills=skill_bindings)
        return refs

    def declared(self, source, package, index_path, source_paths):
        """Inventory a closed Markdown subtree and prove each declared source call site."""
        from .text import parse_index, render_text
        from qorqut_operations.contracts import DomainError

        if not source.exists(index_path):
            return
        self.put(source, package, index_path, kind="metadata")
        entries = parse_index(source.read(index_path))
        prefix = posixpath.dirname(index_path) + "/"
        indexed = set()
        trees, sites = {}, {}
        for entry in entries:
            path, site = entry["path"], entry["source_site"]
            source_path, symbol = site.split(":")
            if not path.startswith(prefix) or source_path not in source_paths:
                raise ImportFailure("invalid_text_index", "Declaration is outside the bounded source contract", path=path, source_site=site)
            text = source.read(path).decode("utf-8", "strict")
            try:
                render_text(text, entry["parameters"], {name: "" for name in entry["parameters"]})
            except DomainError as exc:
                raise ImportFailure("invalid_text_index", exc.message, path=path) from exc
            raw = source.read(source_path)
            # Several fragments share one source site. Reuse only parsing of
            # exact bytes read during this inventory; validate each call below.
            site_key = (raw, symbol)
            if site_key not in sites:
                if raw not in trees:
                    trees[raw] = ast.parse(raw.decode("utf-8", "strict"))
                nodes = [trees[raw]]
                for part in symbol.split("."):
                    found = []
                    for parent in nodes:
                        for node in getattr(parent, "body", []):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == part:
                                found.append(node)
                            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                                if any(isinstance(target, ast.Name) and target.id == part for target in targets):
                                    found.append(node)
                    nodes = found
                sites[site_key] = [n for root in nodes for n in ast.walk(root) if isinstance(n, ast.Call)
                                  and isinstance(n.func, ast.Name) and n.func.id in {"render", "bundled"}
                                  and len(n.args) >= 2 and isinstance(n.args[-1], ast.Constant)]
            calls = [n for n in sites[site_key] if n.args[-1].value == path]
            valid = [n for n in calls if len(n.args) == (3 if n.func.id == "render" else 2)
                     and isinstance(n.args[-2], ast.Constant) and n.args[-2].value == source.id
                     and {k.arg for k in n.keywords} == set(entry["parameters"])]
            if not valid or len(valid) != len(calls):
                raise ImportFailure("unresolved_source_site", "No exact renderer call with declared parameters at source_site", path=path, source_site=site)
            self.entry(source, package, path, "instruction", entry_id=entry["id"])
            row = next(a for a in self.assets if a["source_id"] == source.id and a["path"] == path)
            row.update(parameters=entry["parameters"], source_site=site,
                       source_site_sha256=digest(raw), source_site_lines=sorted(n.lineno for n in valid))
            self.declarations[(source.id, path)] = entry
            self.composition.setdefault((source.id, source_path), []).append(dict(
                asset_path=path, parameters=entry["parameters"], source_site=site,
                lines=sorted(n.lineno for n in valid), source_sha256=digest(raw)))
            indexed.add(path)
        for path in source.tree(prefix.rstrip("/")):
            if path.endswith(".md") and path not in indexed:
                source.issue("undeclared_instruction", "Markdown fragment is absent from its index", path=path)

    def _declared_calls(self, source, path, tree):
        """Return only independently checked calls; unknown render calls stay pending."""
        valid, unresolved = set(), []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id not in {"render", "bundled"}:
                continue
            # Email's byte renderer is a separate explicit contract, not a Markdown index call.
            if path.endswith("/bootstrap/email_guidance.py"):
                continue
            count = 3 if node.func.id == "render" else 2
            args = node.args
            declaration = self.declarations.get((source.id, args[-1].value)) if args and isinstance(args[-1], ast.Constant) else None
            if (len(args) == count and declaration is not None
                    and isinstance(args[-2], ast.Constant) and args[-2].value == source.id
                    and {k.arg for k in node.keywords} == set(declaration["parameters"])):
                valid.update(id(child) for child in ast.walk(node))
            else:
                unresolved.append(dict(line=node.lineno, status="pending_host_composition", reason="undeclared renderer call"))
        return valid, unresolved

    def builtin_skill_composition(self, source, package, path, tree):
        skill_path = {
            "app/bazarlyk/agents/role_workflow.py": "app/bazarlyk/agents/skills/role/SKILL.md",
            "app/bazarlyk/agents/cleanup_workflow.py": "app/bazarlyk/agents/skills/cleanup/SKILL.md",
        }.get(path)
        functions = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
        if skill_path is None or "skill_files" not in functions:
            return
        calls = [n for n in ast.walk(functions["skill_files"]) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name) and n.func.id == "read_resource"
                 and len(n.args) == 3 and ast.unparse(n.args[0]) == "agentic_view"
                 and isinstance(n.args[1], ast.Constant) and n.args[1].value == "host"
                 and isinstance(n.args[2], ast.Constant) and n.args[2].value == skill_path]
        compatible = all(name in functions and "agentic_view" in {a.arg for a in functions[name].args.kwonlyargs}
                         for name in ("skill_files", "skill_metadata"))
        if not calls or not compatible or "native/" + skill_path not in package["files"]:
            source.issue("pending_host_composition", "Built-in skill lacks its managed resource contract", path=path, severity="pending")
            return
        self.composition.setdefault((source.id, path), []).append(dict(asset_path=skill_path, parameters=[],
            source_site=path + ":skill_files", lines=[n.lineno for n in calls], source_sha256=digest(source.read(path))))

    def email_guide(self, source, package, root):
        """Import the canonical guide from explicit offline bytes, never instance discovery."""
        from bazarlyk.bootstrap import email_guidance
        from .text import render_text
        from qorqut_operations.contracts import DomainError

        prefix = "skills/email-management/instance-template/"
        if root is not None:
            # An authored snapshot is not a factory template: it needs only the common entry files and keeps whatever else it holds.
            required = set(email_guidance.GUIDE_FILES)
            source = Source("email-guidance", root, source.owner, source.enabled, self.diagnostics)
            self.sources[source.id] = source
            prefix = ""
            files = source.tree("", root_resources=True)
        else:
            version = read_yaml(source.read("plugin.yaml")).get("version")
            required = email_guidance.template_files(version)
            if required is None:
                raise ImportFailure("unsupported_email_guidance", "Email Management package version has no known factory template",
                                    version=str(version))
            files = [prefix + path for path in sorted(required)]
        relative_files = {path.removeprefix(prefix) for path in files}
        if not required <= relative_files:
            raise ImportFailure("missing_email_guidance", "Offline email guide lacks required files",
                                missing=sorted(required - relative_files))
        if len(files) > 2048:
            raise ImportFailure("oversized_resource", "Offline email guide has too many resources")
        size = 0
        for path in files:
            name = path.removeprefix(prefix)
            data = source.read(path)
            size += len(data)
            if size > 64 * 1024 * 1024:
                raise ImportFailure("oversized_resource", "Offline email guide exceeds 64 MiB")
            if path.endswith(".md"):
                try:
                    text = data.decode("utf-8", "strict")
                    parameters = ["EMAIL_MANAGEMENT_HOME"] if "{{EMAIL_MANAGEMENT_HOME}}" in text else []
                    render_text(text, parameters, {parameter: "" for parameter in parameters})
                except (UnicodeError, DomainError) as exc:
                    raise ImportFailure("invalid_email_guidance", "Offline email guidance has invalid text/placeholders", path=path) from exc
            kwargs = dict(destination="guidance/email/" + name, data=data,
                          symbol="email-guidance" if source.id == "email" else None)
            if name == "SKILL.md":
                self.entry(source, package, path, "instruction", entry_id="email-guidance", **kwargs)
            else:
                self.put(source, package, path, **kwargs)

    def email_composition(self, source, path, tree):
        """Check the explicit email source contract and canonical home lookup without execution."""
        from .text import render_text
        from qorqut_operations.contracts import DomainError

        expected = dict(schema_version=1, parameters=["EMAIL_MANAGEMENT_HOME"],
                        discovery_source="skills/email-management/SKILL.md",
                        canonical_package="imported-email-management", canonical_path="guidance/email/SKILL.md")
        declarations = [node for node in tree.body if isinstance(node, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "EMAIL_COMPOSITION" for t in node.targets)]
        try:
            declared = ast.literal_eval(declarations[0].value) if len(declarations) == 1 else None
        except (TypeError, ValueError):
            declared = None
        functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
        if declared != expected or not {"render", "home", "skill_files"} <= functions.keys():
            return False
        if any("agentic_view" not in {a.arg for a in functions[name].args.kwonlyargs} for name in ("render", "home", "skill_files")):
            return False
        # A declared name alone is not coverage. Verify the actual one-pass implementation
        # shape and canonical lookup; these AST expressions are compared, never evaluated.
        render_body = functions["render"].body
        if render_body and isinstance(render_body[0], ast.Expr) and isinstance(render_body[0].value, ast.Constant):
            render_body = render_body[1:]
        expected_render = ast.parse('''text = body.decode("utf-8", "strict")
parameters = ("EMAIL_MANAGEMENT_HOME",) if PLACEHOLDER in text else ()
values = {"EMAIL_MANAGEMENT_HOME": str(home(paths, agentic_view=agentic_view))} if parameters else {}
return render_text(text, parameters, values).encode("utf-8")''').body
        if [ast.dump(n) for n in render_body] != [ast.dump(n) for n in expected_render]:
            return False
        home_calls = [n for n in ast.walk(functions["home"]) if isinstance(n, ast.Call)
                      and ast.unparse(n.func) == "agentic_view.layer.catalog.read_resource"]
        if not any([arg.value for arg in n.args if isinstance(arg, ast.Constant)] == [
                expected["canonical_package"], expected["canonical_path"]] for n in home_calls):
            return False
        home_returns = [n for n in ast.walk(functions["home"]) if isinstance(n, ast.Return)]
        if len(home_returns) != 1 or ast.unparse(home_returns[0].value) != "Path(agentic_view.layer.root) / Path(root_path).parent":
            return False
        email = self.sources.get("email")
        if email is None:
            return False
        try:
            discovery = email.read(expected["discovery_source"]).decode("utf-8", "strict")
            render_text(discovery, expected["parameters"], {"EMAIL_MANAGEMENT_HOME": ""})
        except (UnicodeError, DomainError, ImportFailure):
            return False
        self.composition[(source.id, path)] = [dict(**expected, source_sha256=digest(source.read(path)),
                                                   status="declared_email_composition")]
        return True

    def isolate_templates(self):
        """Separate inactive seeds/provenance while retaining exact native relative paths."""
        for package in list(self.packages.values()):
            package_id = package["id"]
            rows = [row for row in self.assets if row["package_id"] == package_id and
                    (row["template"] or "/native/provenance-snapshot/" in row["destination"])]
            if not rows:
                continue
            inactive_id = package_id + "-templates"
            inactive = self.package(inactive_id, package["native"]["owner"], False)
            moved = {row["destination"].removeprefix(f"{package['path']}/") for row in rows}
            for path in moved:
                inactive["files"][path] = package["files"].pop(path)
            retained = []
            refs = {}
            for entry in package["entries"]:
                if entry["path"] in moved:
                    entry["native"].update(template=True, enabled=False)
                    if entry["kind"] != "template":
                        entry["kind"] = "template"
                    inactive["entries"].append(entry)
                    refs[f"{package_id}/{entry['id']}"] = f"{inactive_id}/{entry['id']}"
                else:
                    retained.append(entry)
            package["entries"] = retained
            for row in rows:
                row.update(package_id=inactive_id, destination=row["destination"].replace(
                    f"{package['path']}/", f"{inactive['path']}/", 1), enabled=False, template=True)
            for binding in self.bindings:
                binding["entry_refs"] = [refs.get(ref, ref) for ref in binding["entry_refs"]]
                for skill in binding.get("skills", []):
                    skill["canonical_entry"] = refs.get(skill["canonical_entry"], skill["canonical_entry"])
                    if binding["package_id"] == package_id and skill.get("provenance_snapshot") in moved:
                        skill["provenance_package_id"] = inactive_id
                if binding["package_id"] == package_id and binding.get("template"):
                    binding.update(package_id=inactive_id, enabled=False)
                elif binding["package_id"] == package_id:
                    binding["entry_refs"] = [ref for ref in binding["entry_refs"] if not ref.startswith(inactive_id + "/")]

    def embedded(self, source, package, path, *, metadata=False):
        raw = source.read(path)
        try:
            text = raw.decode("utf-8")
            tree = ast.parse(text)
        except (UnicodeError, SyntaxError) as exc:
            raise ImportFailure("invalid_python", "Cannot parse instruction source", path=path) from exc
        covered, records = self._declared_calls(source, path, tree)
        self.builtin_skill_composition(source, package, path, tree)
        email_contract = path.endswith("/bootstrap/email_guidance.py") and self.email_composition(source, path, tree)
        has_contract = bool(self.composition.get((source.id, path)))
        # Operational validation errors are diagnostics, never instruction fragments.
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "DomainError":
                covered.update(id(child) for child in ast.walk(node))
        for container in ast.walk(tree):
            if isinstance(container, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if container.body and isinstance(container.body[0], ast.Expr):
                    covered.add(id(container.body[0].value))  # Documentation is not an authored prompt.
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            name = ast.unparse(targets[0])
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                value = None
            is_instruction = any(word in name.upper() for word in ("INSTRUCTIONS", "GUIDANCE", "NOTE", "PROMPT"))
            if id(node.value) in covered:
                continue
            if isinstance(value, str) and is_instruction and value:
                if has_contract:
                    records.append(dict(symbol=name, line=node.lineno, status="pending_host_composition",
                                        reason="New embedded instruction has no Markdown declaration"))
                    continue
                destination = f"native/extracted/{path}/{name}.txt"
                self.entry(source, package, path, "instruction", data=value.encode(), destination=destination,
                           symbol=name, line=node.lineno)
                covered.update(id(child) for child in ast.walk(node.value))
            elif not metadata and (is_instruction or name in {"work", "bundle", "orientation"}) and value is None and not has_contract:
                records.append(dict(symbol=name, line=node.lineno,
                                    expression=ast.get_source_segment(text, node.value),
                                    status="pending_host_composition"))
            elif metadata and isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in targets):
                # Source expressions are evidence only; no eval, imported module, or executable tool entry.
                records.append(dict(symbol=name, line=node.lineno, expression=ast.get_source_segment(text, node.value),
                                    literal=value, status="native_metadata"))
        if metadata:
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "orientation":
                    calls = [n for n in ast.walk(node) if isinstance(n, ast.Call) and id(n) in covered]
                    leftovers = [n for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                                 and n.value and id(n) not in covered]
                    if not calls or leftovers:
                        records.append(dict(symbol=node.name, line=node.lineno,
                                            expression=ast.get_source_segment(text, node),
                                            status="pending_host_composition"))
                        source.issue("pending_host_composition", "Native orientation lacks complete declared composition",
                                     path=path, severity="pending")
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "guidance":
                    leftovers = [n for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
                                 and n.value and id(n) not in covered]
                    if has_contract and leftovers:
                        source.issue("pending_host_composition", "Native guidance contains undeclared prose", path=path, severity="pending")
                    returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
                    if len(returns) == 1:
                        try:
                            value = ast.literal_eval(returns[0].value)
                        except (ValueError, TypeError):
                            value = None
                        if isinstance(value, str):
                            self.entry(source, package, path, "instruction", data=value.encode(), symbol=node.name,
                                       line=returns[0].lineno, destination=f"native/extracted/{path}/guidance.txt")
                            covered.update(id(n) for n in ast.walk(returns[0]))
            # Record operation/connection declaration calls as non-executable metadata.
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
                        "OperationDescriptor", "CatalogEntry", "NativeConnection", "CalendarProvider"}:
                    records.append(dict(line=node.lineno, expression=ast.get_source_segment(text, node),
                                        status="native_metadata"))
        else:
            # Long authored fragments inside composition logic are not a fully rendered prompt.
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) >= 80
                        and id(node) not in covered and node is not getattr(tree.body[0], "value", None)):
                    records.append(dict(line=node.lineno, text=node.value, status="pending_host_composition"))
            if path.endswith("/bootstrap/email_guidance.py"):
                # The only rendered instruction is the supplied native skill with an instance path substitution.
                if not email_contract:
                    records = [dict(symbol=node.name, line=node.lineno,
                                    expression=ast.get_source_segment(text, node), status="pending_host_composition")
                               for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render"]
            if records:
                source.issue("pending_host_composition", "Dynamic host instruction composition requires later integration",
                             path=path, severity="pending")
        if metadata and any(r["status"] == "pending_host_composition" for r in records) and not any(
                d["code"] == "pending_host_composition" and d.get("path") == path for d in source.diagnostics):
            source.issue("pending_host_composition", "Undeclared native composition", path=path, severity="pending")
        if has_contract:
            from .support import canonical
            self.put(source, package, path, kind="metadata", data=canonical({
                "status": "declared_composition", "source_sha256": digest(raw),
                "contracts": self.composition[(source.id, path)]}),
                destination=f"native/extracted/{path}/composition.json", status="declared_composition")
        if records:
            from .support import canonical
            self.put(source, package, path, kind="metadata", data=canonical(records),
                     destination=f"native/extracted/{path}/source-records.json",
                     status="pending_host_composition" if any(r["status"] == "pending_host_composition" for r in records) else "metadata")

    def links(self):
        for package in self.packages.values():
            for path, item in package["files"].items():
                if not path.endswith(".md") or "/provenance-snapshot/" in path:
                    continue
                text = decoded(item["content_base64"]).decode("utf-8", "replace")
                for link in re.findall(r"\]\(([^\s)]+)(?:\s+[^)]*)?\)", text):
                    url = urlsplit(link.strip("<>"))
                    if url.scheme or url.netloc or not url.path:
                        continue
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), unquote(url.path)))
                    if target not in package["files"] and not any(p.startswith(target.rstrip("/") + "/") for p in package["files"]):
                        self.diagnostics.append(dict(code="missing_resource", message="Referenced local resource is not preserved",
                            source_id=item["provenance"]["source_id"], path=item["provenance"]["path"],
                            target=target, severity="error"))

    def finish(self):
        self.isolate_templates()
        self.links()
        for package in self.packages.values():
            package["entries"].sort(key=lambda entry: entry["id"])
            package["files"] = dict(sorted(package["files"].items()))
        for asset in self.assets:
            if asset["kind"] in {"skill", "template"}:
                prefix = posixpath.dirname(asset["destination"]) + "/"
                asset["resources"] = sorted({row["destination"] for row in self.assets
                                             if row["destination"].startswith(prefix)
                                             and row["destination"] != asset["destination"]})
            package = self.packages[asset["package_id"]]
            refs = {f"{package['id']}/{entry['id']}" for entry in package["entries"]
                    if asset["destination"] == f"{package['path']}/{entry['path']}"
                    or (entry["kind"] in {"skill", "template"} and asset["destination"].startswith(
                        f"{package['path']}/" + posixpath.dirname(entry["path"]) + "/"))}
            asset["native_bindings"] = [{"kind": b["kind"], "native_id": b["native_id"]}
                                        for b in self.bindings if refs.intersection(b["entry_refs"])
                                        or (not refs and b["package_id"] == package["id"])]
        records = [self.sources[key].record() for key in sorted(self.sources)]
        errors = [issue for issue in self.diagnostics if issue["severity"] == "error"]
        pending = [issue for issue in self.diagnostics if issue["severity"] == "pending"]
        return dict(schema_version=2, sources=records, packages=list(self.packages.values()),
                    bindings=self.bindings, assets=self.assets, diagnostics=self.diagnostics,
                    coverage=dict(complete=not errors and not pending, errors=len(errors), pending=len(pending),
                                  asset_count=len(self.assets), package_count=len(self.packages)),
                    source_digest=digest(records))


def _compose_inventory(inv, selected, custom_sources, email_guidance_root):
    """Compose inventory from supplied source readers without executing source code."""
    if "roles" in inv.sources:
        source = inv.sources["roles"]
        role_package = inv.package("imported-roles", source.owner, True)
        skill_packages = {"operations": inv.package("imported-role-skills", source.owner, True),
                          "overseer": inv.package("overseer-role-skills", source.owner, True)}
        for role in ROLE_IDS:
            def import_role(role_id):
                path = f"roles/imported-roles/native/roles/{role_id}/role.yaml"
                manifest = read_yaml(source.read(path))
                native_id = identifier(manifest.get("id"))
                if native_id != role_id:
                    raise ImportFailure("invalid_manifest", "Role ID does not match its path", path=path)
                inv.put(source, role_package, path, kind="manifest", destination=f"native/roles/{role_id}/role.yaml")
                brief = relative(manifest.get("brief"))
                if not brief.startswith("roles/imported-roles/native/roles/" + role_id + "/"):
                    raise ImportFailure("invalid_manifest", "Role brief is outside its bundle", path=brief)
                logical = brief.removeprefix("roles/imported-roles/")
                refs = [inv.entry(source, role_package, brief, "instruction", destination=logical)]
                skills = []
                for skill in manifest.get("skills", []):
                    skill_id = identifier(skill.get("id"))
                    skill_path = relative(skill.get("path"))
                    area = "overseer" if skill_id == "learning-application" else "operations"
                    package = skill_packages[area]
                    prefix = package["path"] + "/"
                    if not skill_path.startswith(prefix + "native/skills/") or not skill_path.endswith("/SKILL.md"):
                        raise ImportFailure("invalid_manifest", "Role skill is outside its canonical package", path=skill_path)
                    logical_skill = skill_path.removeprefix(prefix)
                    for item in source.tree(posixpath.dirname(skill_path)):
                        target = item.removeprefix(prefix)
                        if item == skill_path:
                            ref = inv.entry(source, package, item, destination=target)
                        else:
                            inv.put(source, package, item, destination=target)
                    refs.append(ref)
                    skills.append(dict(native_skill_id=skill_id, canonical_entry=ref))
                inv.binding(source, native_id, role_package, refs, kind="role", manifest_path=path, skills=skills)
            inv.attempt(source, import_role, role)
        # The shipped manifests are the canonical bundle definitions. Preserve
        # their exact bytes while independently checking their inventory bindings.
        for package in (role_package, *skill_packages.values()):
            def import_bundle_manifest(package):
                path = package["path"] + "/agentic.yaml"
                body = source.read(path)
                manifest = read_yaml(body)
                if manifest.get("id") != package["id"] or not isinstance(manifest.get("entries"), list):
                    raise ImportFailure("invalid_manifest", "Invalid bundled package manifest", path=path)
                supplied = {(e.get("id"), e.get("kind"), e.get("path")) for e in manifest["entries"]}
                expected = {(e["id"], e["kind"], e["path"]) for e in package["entries"]}
                if supplied != expected or len(supplied) != len(manifest["entries"]):
                    raise ImportFailure("invalid_manifest", "Bundle entries disagree with native bindings", path=path)
                package["manifest_file"] = dict(content_base64=encoded(body), sha256=digest(body),
                                                source_id=source.id, source_path=path)
            inv.attempt(source, import_bundle_manifest, package)
        def import_runtime_instructions():
            package = inv.package("runtime-instructions", source.owner, True)
            prefix = package["path"] + "/"
            body = source.read(prefix + "agentic.yaml")
            manifest = read_yaml(body)
            if manifest.get("id") != package["id"] or not isinstance(manifest.get("entries"), list):
                raise ImportFailure("invalid_manifest", "Invalid runtime instruction manifest")
            refs = []
            for entry in manifest["entries"]:
                path = relative(entry["path"])
                if entry.get("kind") != "instruction" or not path.startswith("messages/"):
                    raise ImportFailure("invalid_manifest", "Invalid runtime instruction entry")
                refs.append(inv.entry(source, package, prefix + path, "instruction",
                                      destination=path, entry_id=entry["id"]))
            for path in ("index.json", "README.md"):
                inv.put(source, package, prefix + path, destination=path)
            package["manifest_file"] = dict(content_base64=encoded(body), sha256=digest(body),
                source_id=source.id, source_path=prefix + "agentic.yaml")
            inv.binding(source, "runtime-instructions", package, refs, kind="builtin")

        inv.attempt(source, import_runtime_instructions)
    if "host" in inv.sources:
        source = inv.sources["host"]
        package = inv.package("imported-host-builtins", source.owner, True)
        inv.attempt(source, inv.tree, source, package, "app/bazarlyk/agents/skills")
        inv.attempt(source, inv.tree, source, package, "app/bazarlyk/bootstrap/instructions", advertise=False, template=True)
        for path in sorted(package["files"]):
            if "/bootstrap/instructions/" in path and path.endswith(".md"):
                inv.entry(source, package, path.removeprefix("native/"), "instruction", template=True)
        inv.attempt(source, inv.declared, source, package, "app/bazarlyk/agentic/assets/index.json",
                    {"app/bazarlyk/" + path for path in HOST_PYTHON})
        for path in HOST_PYTHON:
            inv.attempt(source, inv.embedded, source, package, "app/bazarlyk/" + path)
        inv.binding(source, "host", package, [f"{package['id']}/{e['id']}" for e in package["entries"]], kind="builtin")
        terminal = inv.package("imported-terminal-control", "agent-terminal-control", "agent-terminal-control" in selected)
        # This native manifest deliberately uses app-relative paths.
        inv.attempt(source, inv.native_manifest, source, terminal, "app/plugins/agent-terminal-control/plugin.yaml", base="app")
        template = package
        inv.attempt(source, inv.native_manifest, source, template, "app/templates/plugins/basic/plugin.yaml",
                    base="app/templates/plugins/basic", template=True)
        inv.attempt(source, inv.tree, source, template, "app/templates/plugins/basic", template=True)
    if "email" in inv.sources:
        source = inv.sources["email"]
        package = inv.package("imported-email-management", source.owner, source.enabled)
        inv.attempt(source, inv.native_manifest, source, package, "plugin.yaml")
        inv.attempt(source, inv.email_guide, source, package, email_guidance_root)
        # The original instance copy is inactive provenance, including its nested SKILL.md.
        for entry in package["entries"]:
            if "/instance-template/" in entry["path"]:
                entry["kind"] = "template"
                entry["native"].update(template=True, enabled=False)
        for asset in inv.assets:
            if asset["package_id"] == package["id"] and "/instance-template/" in asset["destination"]:
                asset.update(template=True, enabled=False)
    if "content" in inv.sources:
        source = inv.sources["content"]
        package = inv.package("imported-content", source.owner, source.enabled)
        for skill in CONTENT_SKILLS:
            inv.attempt(source, inv.tree, source, package, "skills/" + skill)
        inv.attempt(source, inv.tree, source, package, "experiences/case-studies", advertise=False)
        inv.attempt(source, inv.put, source, package, "PROVENANCE.md", kind="provenance")
        inv.binding(source, "content", package, [f"{package['id']}/{e['id']}" for e in package["entries"]], kind="extension")
        inv.attempt(source, inv.native_manifest, source, package, "templates/creative-channel/plugin.yaml",
                    base="templates/creative-channel", template=True)
        inv.attempt(source, inv.tree, source, package, "templates/creative-channel", template=True)
        if "content-library" not in inv.sources:
            source.issue("missing_source", "Content requires the supplied content-library metadata source")
    if "content-library" in inv.sources:
        source = inv.sources["content-library"]
        package = inv.packages.get("imported-content")
        if package is None:
            source.issue("missing_source", "Content library requires canonical qorqut-content assets")
        else:
            # Keep each original native path under its own source-relative subtree.
            inv.attempt(source, inv.native_manifest, source, package, "plugin.yaml", canonical=package)
    if "content" in selected and "content" not in inv.sources:
        inv.diagnostics.append(dict(code="missing_source", message="Selected content source was not supplied",
                                    source_id="content", path=None, severity="error"))
    for source_id in ("paper", "fastmail"):
        if source_id not in inv.sources:
            if source_id in selected:
                inv.diagnostics.append(dict(code="missing_source", message="Selected extension source was not supplied",
                                            source_id=source_id, path=None, severity="error"))
            continue
        source = inv.sources[source_id]
        package = inv.package(f"imported-{source_id}-metadata", source.owner, source.enabled)
        paths = ("qorqut_paper/native.py", "qorqut_paper/extension.py", "qorqut_paper/outreach.py") if source_id == "paper" else (
            "qorqut_fastmail/operations.py", "qorqut_fastmail/extension.py")
        if source_id == "paper":
            inv.attempt(source, inv.declared, source, package, "qorqut_paper/agentic-assets/index.json", set(paths))
        else:
            inv.attempt(source, inv.declared, source, package, "qorqut_fastmail/resources/index.json", set(paths))
        for path in paths:
            inv.attempt(source, inv.embedded, source, package, path, metadata=True)
        for tree in ("docs", "skills"):
            if source.exists(tree):
                inv.attempt(source, inv.tree, source, package, tree, advertise=tree == "skills")
        inv.binding(source, source_id, package, [f"{package['id']}/{e['id']}" for e in package["entries"]],
                    kind="extension", metadata_only=not package["entries"])
        source.issue("metadata_only" if not package["entries"] else "metadata_and_guidance",
                     "No standalone skill is supplied; provider code is excluded", severity="info")
    for source_id, guidance_path in (("telegram", "qorqut_telegram/resources/telegram-replies.md"),
                                     ("whatsapp", "qorqut_whatsapp/guidance.md")):
        if source_id not in inv.sources:
            if source_id in selected:
                inv.diagnostics.append(dict(code="missing_source", message="Selected connector guidance source was not supplied",
                                            source_id=source_id, path=None, severity="error"))
            continue
        source = inv.sources[source_id]
        package = inv.package(f"imported-{source_id}", source.owner, source.enabled)
        index_path = "qorqut_telegram/resources/index.json" if source_id == "telegram" else "qorqut_whatsapp/index.json"
        inv.attempt(source, inv.declared, source, package, index_path, {f"qorqut_{source_id}/extension.py"})
        if not package["entries"]:
            source.issue("missing_resource", "Selected connector has no declared guidance", path=index_path)
        inv.binding(source, source_id, package, [f"{package['id']}/{entry['id']}" for entry in package["entries"]], kind="extension")
    if not isinstance(custom_sources, (list, tuple)):
        raise ImportFailure("invalid_sources", "custom_sources must be explicit descriptors")
    for descriptor in custom_sources:
        if (not isinstance(descriptor, dict) or set(descriptor) != {"id", "root", "owner", "kind", "enabled"}
                or descriptor["kind"] not in {"plugin", "skill"} or not isinstance(descriptor["enabled"], bool)
                or not isinstance(descriptor["owner"], str) or not descriptor["owner"]):
            raise ImportFailure("invalid_sources", "Custom descriptor requires id/root/owner/kind/enabled")
        source_id = "custom-" + identifier(descriptor["id"])
        if source_id in inv.sources:
            raise ImportFailure("duplicate_native_id", "Duplicate custom source ID", source_id=source_id)
        source = Source(source_id, descriptor["root"], descriptor["owner"], descriptor["enabled"], inv.diagnostics)
        inv.sources[source_id] = source
        package = inv.package("imported-" + source_id, source.owner, source.enabled)
        if descriptor["kind"] == "plugin":
            inv.attempt(source, inv.native_manifest, source, package, "plugin.yaml")
        else:
            if source.exists("SKILL.md"):
                ref = inv.entry(source, package, "SKILL.md")
                # The supplied root is the resource boundary, with private/runtime exclusions.
                inv.attempt(source, inv.tree, source, package, "", advertise=False)
                inv.binding(source, descriptor["id"], package, [ref], kind="skill")
            else:
                source.issue("missing_resource", "Custom skill root has no SKILL.md", path="SKILL.md")
    # Detect extra skill/manifests in supplied native asset areas. Source code, docs and tests are not asset roots.
    for source_id, source in inv.sources.items():
        roots = {"host": ("app/bazarlyk/agents/skills", "app/plugins", "app/templates/plugins"),
                 "roles": ("roles/imported-roles/native/roles", "operations/imported-role-skills/native/skills", "overseer/overseer-role-skills/native/skills"), "content": ("skills", "templates"),
                 "email": ("skills",), "content-library": ("skills",)}.get(source_id, ())
        if source_id.startswith("custom-"):
            roots = ("skills", "critic", "templates")
        for root in roots:
            if not source.exists(root):
                continue
            paths = inv.attempt(source, source.tree, root) or []
            for path in paths:
                if path.rsplit("/", 1)[-1] in {"SKILL.md", "role.yaml", "plugin.yaml"} and path not in source.classified:
                    source.issue("unclassified_asset", "Candidate asset has not been classified", path=path)
    return deepcopy(inv.finish())


def inventory_sources(source_roots, selected_extensions, custom_sources=(), *, email_guidance_root=None, export_root=None, installed_sources=()):
    """Inventory supplied roots. Source IDs: host, roles, email; optional content,
    content-library, paper, fastmail. Custom descriptors: id/root/owner/kind/enabled.
    All output paths are relative. No source Python is ever imported or executed.
    """
    if not isinstance(source_roots, dict) or set(source_roots) - SOURCE_IDS:
        raise ImportFailure("invalid_sources", "Unknown source IDs", allowed=sorted(SOURCE_IDS))
    if not isinstance(selected_extensions, (list, tuple, set)) or any(not isinstance(s, str) for s in selected_extensions):
        raise ImportFailure("invalid_selection", "selected_extensions must be an explicit collection of IDs")
    selected = set(selected_extensions)
    from .installed_sources import installed_source, CONNECTORS
    if set(installed_sources) - (CONNECTORS & selected) or set(installed_sources) - source_roots.keys():
        raise ImportFailure("invalid_selection", "Installed instruction sources must be explicitly selected connectors")
    installed = {key: installed_source(key) for key in installed_sources}
    if selected - {"content", "paper", "fastmail", "telegram", "whatsapp", "email-management", "agent-terminal-control"}:
        raise ImportFailure("invalid_selection", "Unknown selected extension", ids=sorted(selected))
    export_components, manifest_digest = {}, None
    if export_root is not None:
        from bazarlyk.source_snapshot import validate_export
        try:
            export_root = absolute(export_root)
            manifest, manifest_digest = validate_export(export_root)
            by_name = {c["name"]: c for c in manifest["components"]}
            names = {"host": "qorqut-code", "roles": "qorqut-agentic", "email": "email-management",
                     "content": "qorqut-content", "content-library": "content-library", "paper": "qorqut-paper", "fastmail": "qorqut-fastmail", "telegram": "qorqut-code", "whatsapp": "qorqut-code"}
            for key, path in source_roots.items():
                if key in installed:
                    if absolute(path) != absolute(installed[key]["root"]):
                        raise ValueError("Installed source root does not match its distribution: " + key)
                    continue
                component = by_name.get(names[key])
                expected = export_root / component["destination"] if component else None
                if key == "roles" and expected is not None:
                    expected = expected / "src/qorqut_agentic/resources/defaults"
                elif key in {"telegram", "whatsapp"} and expected is not None:
                    expected = expected / "extensions" / key
                if component is None or absolute(path) != expected:
                    raise ValueError("Source root does not match its export component: " + key)
                export_components[key] = component
        except (OSError, ValueError) as exc:
            raise ImportFailure("invalid_export", str(exc)) from exc
    inv = Inventory()
    for required in ("host", "roles", "email"):
        if required not in source_roots:
            inv.diagnostics.append(dict(code="missing_source", message="Required source was not supplied",
                                        source_id=required, path=None, severity="error"))
    for source_id, root in source_roots.items():
        owner = {"email": "email-management", "content-library": "content"}.get(source_id, source_id)
        enabled = source_id in {"host", "roles"} or owner in selected
        try:
            inv.sources[source_id] = Source(source_id, root, owner, enabled, inv.diagnostics,
                export_component=export_components.get(source_id), manifest_digest=manifest_digest,
                installed_distribution=installed.get(source_id))
        except ImportFailure as exc:
            inv.diagnostics.append(dict(code=exc.code, message=exc.message, source_id=source_id,
                                        severity="error", **exc.details))
    result = _compose_inventory(inv, selected, custom_sources, email_guidance_root)
    if any(installed_source(key) != value for key, value in installed.items()):
        raise ImportFailure("installed_source_changed", "Installed wheel RECORD changed during capture")
    if export_root is not None:
        # Reject any edit while the used assets were being captured; never
        # turn newly observed bytes into a replacement expected digest.
        try:
            after, after_digest = validate_export(export_root)
            if after != manifest or after_digest != manifest_digest:
                raise ValueError("Export manifest changed during capture")
        except (OSError, ValueError) as exc:
            raise ImportFailure("invalid_export", str(exc)) from exc
    return result
