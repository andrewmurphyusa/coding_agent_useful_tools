# Copyright (c) 2026 Andrew Murphy
#
# This file is part of coding-agent-useful-tools.
#
# coding-agent-useful-tools is dual-licensed:
# 1. Under the GNU Affero General Public License v3.0 (AGPLv3) for open-source
#    and non-proprietary use. See the LICENSE file in the root directory.
# 2. Under a Commercial License for proprietary use or without AGPLv3 
#    obligations. See COMMERCIAL-LICENSE.md or contact andrew_murphy_datapro@outlook.com
#     with subject line: "Commercial Licensing Request for coding-agent-useful-tools".

import argparse
import ast
import configparser
import difflib
import fnmatch
import json
import math
import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter

from .redact import redact as _redact, redact_object

def redact(text):
    return _redact(text)[0]

def clean(obj):
    return redact_object(obj)

def dump(obj): return json.dumps(clean(obj), ensure_ascii=False, sort_keys=True)

def worktree_root(repo):
    """Find the outermost source boundary without opening worktree contents."""
    root = Path(repo).resolve()
    boundary = root
    for candidate in (root, *root.parents):
        marker = candidate / '.git'
        # Treat even a malformed or dangling metadata alias conservatively as
        # a boundary. Discovering it never requires reading config or gitfiles.
        if marker.exists() or marker.is_symlink():
            # A nested repository's own marker does not remove its parent's
            # read-only boundary. Continue through every enclosing worktree.
            boundary = candidate
    return boundary


def require_source_root(repo, parser=None):
    """Keep scan exclusions and every Git query relative to the same root."""
    repo = Path(repo).resolve()
    if worktree_root(repo) != repo:
        message = 'source repository must be the Git worktree root; nested --repo directories are unsupported'
        if parser is not None:
            parser.error(message)
        raise ValueError(message)
    return repo


def output_path(path, repo, parser):
    """Resolve links before enforcing the read-only source-tree boundary."""
    target = Path(path).resolve()
    repo = worktree_root(repo)
    if target == repo or repo in target.parents:
        parser.error('--out must be outside the source repository')
    if protected_path(target):
        parser.error('--out must be outside ~/.codex and ~/.claude')
    # A second hard link outside the tree could still overwrite a source file.
    if target.is_file() and target.stat().st_nlink > 1:
        parser.error('--out must not overwrite a hard-linked file')
    return target

def protected_path(path):
    """Agent configuration and session roots are never writable outputs."""
    return any(path == root or root in path.parents
               for root in ((Path.home() / name).resolve()
                            for name in ('.codex', '.claude')))

def first_doc(node):
    doc=ast.get_docstring(node)
    return doc.splitlines()[0] if doc else ''
def source(node):
    try: return ast.unparse(node)
    except (ValueError, TypeError): return ''
def name(node): return source(node).split('.')[-1]
def const(node):
    try: return ast.literal_eval(node)
    except (ValueError, TypeError): return None
def kw(call, key):
    return next((x.value for x in call.keywords if x.arg == key),None)
def loc(path,node): return f'{path}:{node.lineno}'
def excluded(path, patterns, is_dir=False):
    parts=path.split('/')
    if any(p in {'.git','.venv','venv','node_modules','__pycache__'} for p in parts): return True
    if any(p.startswith('.env') for p in parts): return True
    for pattern in patterns:
        pattern=pattern.replace('\\','/').removeprefix('./')
        if fnmatch.fnmatchcase(path,pattern) or (pattern.endswith('/**') and (path == pattern[:-3] or path.startswith(pattern[:-2]))): return True
    return False

def git(repo, args, *, isolated=None):
    if args[0] == 'status' and isolated is None:
        return git_status(repo, args)
    if isolated is None:
        repo = require_source_root(repo)
        # Even rev-parse loads local config/includes. Resolve repository paths
        # ourselves, and give every Git subprocess sanitized metadata.
        try:
            root, _, _ = repository_paths(repo)
            temporary_root = Path(tempfile.gettempdir()).resolve()
            if temporary_root == root or root in temporary_root.parents or protected_path(temporary_root):
                return ''
            with tempfile.TemporaryDirectory(prefix='codeclaims-meta-', dir=temporary_root) as directory:
                metadata = Path(directory) / 'metadata'
                if not isolated_metadata(root, metadata): return ''
                return git(root, args, isolated={'GIT_DIR': str(metadata), 'GIT_WORK_TREE': str(root)})
        except OSError:
            return ''
    # Status normally refreshes the index, and core.fsmonitor can execute a
    # repository-configured command. These settings apply to every Git query.
    # Ignore inherited Git config injection, alternate repositories and helpers.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_NO_LAZY_FETCH='1', GIT_CONFIG_NOSYSTEM='1',
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_GLOBAL=os.devnull,
               GIT_ATTR_NOSYSTEM='1')
    if isolated is not None:
        env.update(isolated)
    command = ['git', '-c', 'core.fsmonitor=false', '-c',
               'core.hooksPath=' + os.devnull, '-c', 'core.untrackedCache=false',
               '-c', 'log.showSignature=false',
               '-c', 'core.attributesFile=' + os.devnull,
               '-C', str(repo), *args]
    try:
        result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',errors='replace',check=False,env=env)
        return (result.stdout if '-z' in args else result.stdout.rstrip('\r\n')) if result.returncode == 0 else ''
    except OSError: return ''

def repository_paths(repo):
    """Resolve normal repositories, linked worktrees, and submodule gitfiles."""
    root = Path(repo).resolve()
    for candidate in (root, *root.parents):
        marker = candidate / '.git'
        if marker.exists() and filesystem_link(marker):
            raise OSError('Git metadata aliases are unsupported')
        if marker.is_dir():
            gitdir = marker
        elif marker.is_file():
            text = marker.read_text(encoding='utf-8').strip()
            if not text.startswith('gitdir: '): continue
            gitdir = (candidate / text[8:]).resolve()
        else:
            continue
        common_file = gitdir / 'commondir'
        if common_file.exists() and filesystem_link(common_file):
            raise OSError('Git metadata aliases are unsupported')
        common = (gitdir / common_file.read_text(encoding='utf-8').strip()).resolve() if common_file.is_file() else gitdir
        return candidate, gitdir, common
    raise FileNotFoundError('Git repository metadata not found')


def isolated_metadata(repo, metadata):
    """Copy Git metadata directly; never load config include directives."""
    _, gitdir, common = repository_paths(repo)
    for path in (common / 'config', gitdir / 'HEAD', gitdir / 'index', common / 'packed-refs'):
        if path.exists() and filesystem_link(path): return False
    config_reader = configparser.RawConfigParser(strict=False)
    try:
        config_reader.read_string((common / 'config').read_text(encoding='utf-8'))
        object_format = config_reader.get('extensions', 'objectformat', fallback='sha1')
    except (OSError, configparser.Error):
        return False
    objects = common / 'objects'
    if object_format not in {'sha1', 'sha256'} or '\n' in str(objects) or '\r' in str(objects):
        return False
    metadata.mkdir()
    (metadata / 'objects' / 'info').mkdir(parents=True)
    (metadata / 'refs').mkdir()
    # Alternates are local object reads only; partial clones cannot lazy-fetch.
    # Use Git's quoted path syntax to preserve spaces and Windows backslashes.
    (metadata / 'objects' / 'info' / 'alternates').write_text(
        json.dumps(Path(objects).as_posix(), ensure_ascii=False) + '\n', encoding='utf-8')
    shutil.copyfile(gitdir / 'HEAD', metadata / 'HEAD')
    packed_refs = common / 'packed-refs'
    if packed_refs.is_file(): shutil.copyfile(packed_refs, metadata / 'packed-refs')
    shallow = common / 'shallow'
    if shallow.is_file() and not filesystem_link(shallow): shutil.copyfile(shallow, metadata / 'shallow')
    for source_refs in dict.fromkeys((common / 'refs', gitdir / 'refs')):
        if not source_refs.is_dir() or filesystem_link(source_refs): continue
        for base, dirs, files in os.walk(source_refs, followlinks=False):
            target = metadata / 'refs' / Path(base).relative_to(source_refs)
            target.mkdir(exist_ok=True)
            dirs[:] = sorted(item for item in dirs if not filesystem_link(Path(base) / item))
            for filename in sorted(files):
                source_ref = Path(base) / filename
                if not filesystem_link(source_ref): shutil.copyfile(source_ref, target / filename)
    config = '[core]\nrepositoryformatversion = 0\nbare = false\n'
    if object_format == 'sha256':
        config = '[core]\nrepositoryformatversion = 1\nbare = false\n[extensions]\nobjectFormat = sha256\n'
    (metadata / 'config').write_text(config, encoding='ascii')
    source_index = gitdir / 'index'
    if source_index.is_file():
        shutil.copyfile(source_index, metadata / 'index')
        for shared in sorted(source_index.parent.glob('sharedindex.*')):
            if re.fullmatch(r'sharedindex\.(?:[0-9a-f]{40}|[0-9a-f]{64})', shared.name):
                if not filesystem_link(shared): shutil.copyfile(shared, metadata / shared.name)
    return True


def filesystem_link(path):
    """Reject symlinks and Windows junction/reparse-point aliases."""
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def asset_path(path):
    return any(part in {'templates', 'static'} for part in Path(path).parts)


def text_file(repo, path, patterns):
    """Revalidate saved paths without traversing filesystem aliases."""
    relative = Path(path)
    if relative.is_absolute() or '..' in relative.parts:
        return None
    candidate = repo / relative
    try:
        # Check every component, including intermediate junctions introduced
        # after scan. Checking only the final file misses directory aliases.
        current = Path(candidate.anchor) if candidate.anchor else Path()
        if filesystem_link(current): return None
        for part in candidate.parts[1:] if candidate.anchor else candidate.parts:
            current /= part
            if filesystem_link(current): return None
        resolved = candidate.resolve(strict=True)
        resolved_path = resolved.relative_to(repo.resolve()).as_posix()
        if (asset_path(resolved_path) or excluded(resolved_path, patterns)
                or not resolved.is_file()):
            return None
        return resolved
    except (OSError, ValueError, RuntimeError):
        return None


def index_asset_metadata(metadata):
    """Read cached sizes, never asset blobs, from ordinary Git indexes.

    Return sizes, tracked paths, and whether membership is complete. Split
    indexes need their shared base; report uncertainty rather than guessing.
    """
    try:
        data = (metadata / 'index').read_bytes()
        signature, version, count = struct.unpack_from('!4sII', data)
        if signature != b'DIRC' or version not in {2, 3, 4}: return {}, set(), False
        oid_size = 32 if 'objectFormat = sha256' in (metadata / 'config').read_text(encoding='ascii') else 20
        offset = 12; previous = b''; sizes = {}; tracked = set()
        for _ in range(count):
            start = offset
            size = struct.unpack_from('!I', data, offset + 36)[0]
            flags = struct.unpack_from('!H', data, offset + 40 + oid_size)[0]
            offset += 42 + oid_size
            if flags & 0x4000: offset += 2
            if version == 4:
                byte = data[offset]; offset += 1; remove = byte & 0x7f
                while byte & 0x80:
                    byte = data[offset]; offset += 1
                    remove = ((remove + 1) << 7) | (byte & 0x7f)
                if remove > len(previous): return {}, set(), False
                end = data.index(b'\0', offset)
                path = previous[:len(previous) - remove] + data[offset:end]
                offset = end + 1
            else:
                end = data.index(b'\0', offset)
                path = data[offset:end]
                offset = start + ((end + 1 - start + 7) // 8) * 8
            previous = path
            name = path.decode('utf-8', errors='surrogateescape')
            if asset_path(name):
                tracked.add(name)
                if not flags & 0x3000: sizes[name] = size
        while offset + 8 <= len(data) - oid_size:
            signature, length = struct.unpack_from('!4sI', data, offset)
            if signature == b'link': return {}, set(), False
            offset += 8 + length
        return sizes, tracked, True
    except (OSError, ValueError, IndexError, struct.error):
        return {}, set(), False


def git_status(repo, args, patterns=()):
    """Run Git only on eligible copies, so it cannot open excluded sources.

    Pathspec exclusions alone are insufficient: Git can still read ancestor
    attributes/ignore files and refresh excluded index entries. An isolated
    worktree gives the subprocess no excluded or asset content to read. Nested Git
    worktrees receive their own sanitized metadata, preserving submodule dirt.
    """
    repo = require_source_root(repo)
    try:
        source_root, _, _ = repository_paths(repo)
    except OSError:
        return ''
    temporary_root = Path(tempfile.gettempdir()).resolve()
    # Even an unusual TMPDIR must not cause temporary writes in source/config.
    if temporary_root == source_root or source_root in temporary_root.parents or protected_path(temporary_root):
        return ''
    try:
        with tempfile.TemporaryDirectory(prefix='codeclaims-git-', dir=temporary_root) as directory:
            temporary = Path(directory)
            metadata = temporary / 'metadata'
            mirror = temporary / 'worktree'
            mirror.mkdir()
            if not isolated_metadata(source_root, metadata):
                return ''
            repositories = [(Path(), mirror, metadata)]
            assets = {}
            for base, dirs, files in os.walk(source_root, followlinks=False):
                relative = Path(base).relative_to(source_root)
                destination = mirror / relative
                if not asset_path(relative): destination.mkdir(exist_ok=True)
                if not asset_path(relative) and relative.parts and '.git' in dirs + files:
                    nested = temporary / ('nested-' + str(len(list(temporary.glob('nested-*')))))
                    if isolated_metadata(Path(base), nested):
                        (destination / '.git').write_text('gitdir: ' + nested.as_posix() + '\n', encoding='utf-8')
                        repositories.append((relative, destination, nested))
                dirs[:] = sorted(d for d in dirs if
                    not excluded((relative / d).as_posix(), patterns, True)
                    and not filesystem_link(Path(base) / d))
                for filename in sorted(files):
                    source_file = Path(base) / filename
                    if (excluded((relative / filename).as_posix(), patterns)
                            or filesystem_link(source_file)):
                        continue
                    if asset_path(relative / filename):
                        assets[relative / filename] = source_file.stat().st_size
                        continue
                    shutil.copyfile(source_file, destination / filename)
            # A nested repo's excluded tracked files are absent too. Git would
            # otherwise mark its parent gitlink dirty for those deletions. Ask
            # Git for gitlink commit changes, then propagate only each child's
            # independently filtered worktree dirt up to its parent.
            statuses = {}; extra_untracked = {}
            for relative, destination, repository_metadata in reversed(repositories):
                environment = {'GIT_DIR': str(repository_metadata), 'GIT_WORK_TREE': str(destination)}
                commit_args = [arg.replace('--ignore-submodules=none', '--ignore-submodules=dirty') for arg in args]
                result = git(destination, commit_args, isolated=environment)
                records = {record[3:]: record[:2] for record in result.split('\0') if len(record) > 3
                           and not excluded((relative / record[3:]).as_posix(), patterns)}
                # Assets never enter the mirror. Its artificial deletions are
                # replaced with metadata evidence: M for known size changes,
                # ? for unknowable content, ?? for confirmed untracked paths.
                sizes, tracked, complete = index_asset_metadata(repository_metadata)
                present = {path.relative_to(relative).as_posix(): size for path, size in assets.items()
                           if (relative == path or relative in path.parents)
                           and not any(child != relative and child in path.parents
                                       and relative in child.parents for child, _, _ in repositories)}
                extra_untracked[relative] = set()
                for path, size in sorted(present.items()):
                    status = records.get(path, '  ')
                    if complete and path not in tracked:
                        if status != '  ':
                            # A staged deletion and a new untracked file may
                            # legitimately produce two records for one path.
                            extra_untracked[relative].add(path)
                        else: records[path] = '??'
                    else:
                        # Git can deliberately zero a racy entry's cached size
                        # to force a future content check; zero proves nothing.
                        changed = path in sizes and sizes[path] != 0 and size % (1 << 32) != sizes[path]
                        records[path] = status[0] + ('M' if changed else '?')
                dirty_children = []
                for child, _, _ in repositories:
                    if child == relative or relative not in child.parents or not (statuses.get(child) or extra_untracked.get(child)): continue
                    if any(parent != relative and parent != child and relative in parent.parents
                           and parent in child.parents for parent, _, _ in repositories): continue
                    dirty_children.append(child.relative_to(relative).as_posix())
                if dirty_children:
                    unfiltered = git(destination, args, isolated=environment)
                    child_markers = {record[3:]: record[:2] for record in unfiltered.split('\0') if len(record) > 3}
                    for child in dirty_children:
                        marker = child_markers.get(child)
                        if marker and marker[1] == 'M':
                            current = records.get(child, '  ')
                            child_relative = relative / child
                            certain = bool(extra_untracked.get(child_relative)) or any(
                                state == '??' or state[0] not in {' ', '?'} or state[1] not in {' ', '?'}
                                for state in statuses[child_relative].values())
                            records[child] = current[0] + ('M' if certain else '?')
                statuses[relative] = records
            entries = [(path, status) for path, status in statuses[Path()].items()]
            entries.extend((path, '??') for path in extra_untracked[Path()])
            return '\0'.join(status + ' ' + path for path, status in
                             sorted(entries, key=lambda item: (item[1] == '??', item[0], item[1])))
    except OSError:
        return ''

def gitfacts(repo, patterns=()):
    repo = require_source_root(repo)
    commits=[]
    for line in git(repo,['log','-50','--format=%cs%x09%h%x09%s']).splitlines():
        pieces=line.split('\t',2)
        if len(pieces)==3: commits.append(dict(zip(('date','sha','subject'),pieces)))
    dates=git(repo,['log','--reverse','--format=%cs']).splitlines()
    dirty=[]
    for line in git_status(repo, ['status', '--porcelain=v1', '-z', '--untracked-files=all',
                                 '--ignore-submodules=none', '--no-renames'], patterns).split('\0'):
        if len(line)>3: dirty.append({'status':line[:2], 'path':line[3:]})
    return {'branch':git(repo,['branch','--show-current']), 'head':git(repo,['rev-parse','--short=12','HEAD']), 'total_commits':int(git(repo,['rev-list','--count','HEAD']) or 0), 'first_date':dates[0] if dates else None,'last_date':dates[-1] if dates else None,'commits':commits,'dirty':dirty,
            'status_notes': ['A ? worktree position means possible asset changes: contents were not read; tracking is also unknown when index membership is unavailable.',
                             'Asset ?? means the path is absent from the index; asset ignore rules are not evaluated, so ignored untracked assets may be included.']}

def module_name(path):
    module = path[:-3].replace('/', '.')
    if module == '__init__': return ''
    return module[:-9] if module.endswith('.__init__') else module


def route_prefixes(trees):
    """Resolve static router identities across imports without importing code."""
    imports = {}; routers = {}; registrations = {}
    for path, tree in trees.items():
        module = module_name(path)
        package = module if Path(path).name == '__init__.py' else module.rpartition('.')[0]
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports[(module, alias.asname or alias.name.split('.')[0])] = alias.name if alias.asname else alias.name.split('.')[0]
            elif isinstance(node, ast.ImportFrom):
                parts = package.split('.') if package else []
                base = '.'.join(parts[:len(parts) - node.level + 1]) if node.level else ''
                origin = '.'.join(p for p in (base, node.module) if p)
                for alias in node.names:
                    imports[(module, alias.asname or alias.name)] = '.'.join(p for p in (origin, alias.name) if p)

    def identity(path, expression):
        text = source(expression)
        first, dot, tail = text.partition('.')
        return imports.get((module_name(path), first), '.'.join(p for p in (module_name(path), first) if p)) + (dot + tail if dot else '')

    for path, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(node.value, ast.Call):
                call = node.value
                constructor = name(call.func)
                if constructor not in {'APIRouter', 'Blueprint', 'FastAPI', 'Flask'}: continue
                prefix_node = kw(call, 'prefix') if constructor == 'APIRouter' else kw(call, 'url_prefix')
                prefix = const(prefix_node) if prefix_node is not None else ''
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name): routers[identity(path, target)] = (prefix if isinstance(prefix, str) else '', prefix_node is None or isinstance(prefix, str))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {'include_router', 'register_blueprint'} and node.args:
                option = 'prefix' if node.func.attr == 'include_router' else 'url_prefix'
                prefix_node = kw(node, option)
                prefix = const(prefix_node) if prefix_node is not None else ''
                registrations.setdefault(identity(path, node.args[0]), []).append((identity(path, node.func.value), prefix if isinstance(prefix, str) else '', prefix_node is None or isinstance(prefix, str), node.func.attr == 'register_blueprint' and prefix_node is not None))

    def expand(symbol, seen=frozenset()):
        if symbol in seen: return [('', False)]
        own, resolved = routers.get(symbol, ('', symbol.rsplit('.', 1)[-1] == 'app'))
        parents = registrations.get(symbol)
        if not parents: return [(own, resolved)]
        return [(parent_prefix + prefix + ('' if override else own), resolved and parent_ok and known)
                for parent, prefix, known, override in parents
                for parent_prefix, parent_ok in expand(parent, seen | {symbol})]

    return lambda path, receiver: sorted(set(expand(identity(path, receiver))))


def migration_roots(repo, contents, patterns):
    """Use only already-read config text; never follow external locations."""
    roots = {'migrations/versions'}
    for path, text in contents.items():
        if Path(path).name != 'alembic.ini': continue
        config = configparser.ConfigParser(interpolation=None)
        try: config.read_string(text)
        except configparser.Error: continue
        section = config['alembic'] if config.has_section('alembic') else {}
        script = section.get('script_location', 'migrations')
        versions = section.get('version_locations', '')
        locations = re.split(r'\s+', versions.strip()) if versions.strip() else [script]
        for location in locations:
            uses_here = '%(here)s' in location
            location = location.replace('%(here)s', str(repo / Path(path).parent)).replace('\\', '/')
            candidate = Path(location)
            if not candidate.is_absolute() and not uses_here: candidate = repo / Path(path).parent / candidate
            # Config may name package resources or external directories. Those
            # are outside a static repository scan and are deliberately ignored.
            try: normalized = candidate.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError: continue
            if excluded(normalized, patterns, True): continue
            roots.add(normalized.rstrip('/'))
    return roots


def scan(repo, patterns):
    repo = require_source_root(repo)
    facts={'repo':str(repo.resolve()),'exclude':patterns,'git':gitfacts(repo, patterns),'files':[],'tree':{},'loc':{},'modules':[],'routes':[],'models':[],'settings':[],'env':[],'migrations':[],'migration_chain':[],'migration_branches':False,'tests':[],'dependencies':[],'terraform':[],'assets':[],'entry_points':[]}
    markers=[]; errors=[]; contents={}; trees={}
    for base, dirs, files in os.walk(repo, followlinks=False):
        dirs[:]=sorted(d for d in dirs if not excluded((Path(base)/d).relative_to(repo).as_posix(),patterns,True) and not filesystem_link(Path(base)/d))
        for filename in sorted(files):
            file=Path(base)/filename; path=file.relative_to(repo).as_posix()
            if excluded(path,patterns) or filesystem_link(file): continue
            facts['files'].append(path)
            top=path.split('/')[0] if '/' in path else '.'
            group=facts['tree'].setdefault(top,{'files':0,'extensions':{}}); group['files']+=1
            ext=file.suffix.lower() or '[none]'; group['extensions'][ext]=group['extensions'].get(ext,0)+1
            if asset_path(path):
                facts['assets'].append({'path':path,'bytes':file.stat().st_size}); continue
            if file.suffix.lower() not in {'.py','.tf','.toml','.txt','.ini','.sh','.ps1','.cmd','.js','.ts','.tsx','.jsx','.go','.rs','.java','.c','.h','.cpp','.yaml','.yml','.json'}: continue
            try: text=file.read_text(encoding='utf-8',errors='replace')
            except OSError as exc:
                errors.append({'file':path,'error':str(exc)}); continue
            contents[path]=text
            facts['loc'][ext]=facts['loc'].get(ext,0)+sum(bool(line.strip()) for line in text.splitlines())
            if '/' not in path and ext in {'.sh','.ps1','.cmd'}:
                entry={'name':filename,'path':path}
                if filename=='run.sh': entry['comment']=next((line.lstrip('# ').strip() for line in text.splitlines() if line.startswith('#') and not line.startswith('#!')),'')
                facts['entry_points'].append(entry)
            if file.suffix=='.py':
                try: trees[path]=ast.parse(text,filename=path)
                except SyntaxError as exc: errors.append({'file':path,'line':exc.lineno,'error':exc.msg})
            if file.suffix=='.tf':
                for n,line in enumerate(text.splitlines(),1):
                    m=re.search(r'\b(resource)\s+"([^"]+)"\s+"([^"]+)"|\b(module|variable|output|provider)\s+"([^"]+)"',line)
                    if m: facts['terraform'].append({'kind':m.group(1) or m.group(4),'type':m.group(2) or m.group(4),'name':m.group(3) or m.group(5),'location':f'{path}:{n}'})
            if filename=='pyproject.toml':
                try:
                    data=tomllib.loads(text); project=data.get('project',{}); deps=list(project.get('dependencies',[]))
                    for group in project.get('optional-dependencies',{}).values(): deps.extend(group)
                    poetry=data.get('tool',{}).get('poetry',{})
                    deps += [k+(v if isinstance(v,str) else '') for k,v in poetry.get('dependencies',{}).items() if k.lower()!='python']
                    for k,v in project.get('scripts',{}).items(): facts['entry_points'].append({'name':k,'path':v})
                    facts['dependencies'] += [{'name':re.split(r'[\s\[<>=!~;]',d,maxsplit=1)[0], 'specifier':d,'file':path} for d in deps]
                except (tomllib.TOMLDecodeError,TypeError) as exc: errors.append({'file':path,'error':str(exc)})
            if filename.startswith('requirements') and filename.endswith('.txt'):
                for line in text.splitlines():
                    d=line.strip()
                    if d and not d.startswith(('#','-')): facts['dependencies'].append({'name':re.split(r'[\s\[<>=!~;]',d,maxsplit=1)[0], 'specifier':d,'file':path})
            if ext in {'.py','.tf','.js','.ts','.tsx','.jsx','.go','.rs','.java','.c','.h','.cpp','.sh','.ps1','.cmd'}:
                for n,line in enumerate(text.splitlines(),1):
                    m=re.search(r'\b(TODO|FIXME|XXX|HACK)\b|raise\s+NotImplementedError\b|\bpass\s*#.*(?:stub|todo)',line,re.I)
                    if m: markers.append({'kind':m.group().split()[0].upper(),'location':f'{path}:{n}','text':line.strip()[:160],'enclosing':''})
    prefixes = route_prefixes(trees)
    revision_roots = migration_roots(repo, contents, patterns)
    for path,tree in trees.items():
        module=module_name(path)
        item={'path':path,'module':module,'docstring':first_doc(tree),'classes':[],'functions':[]}
        testnames=[]
        for node in ast.walk(tree):
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'): testnames.append(node.name)
            if isinstance(node,ast.Call) and source(node.func) in {'os.getenv','os.environ.get'} and node.args and isinstance(const(node.args[0]),str): facts['env'].append({'name':const(node.args[0]),'location':loc(path,node)})
            if isinstance(node,ast.Subscript) and source(node.value)=='os.environ' and isinstance(const(node.slice),str): facts['env'].append({'name':const(node.slice),'location':loc(path,node)})
        if Path(path).name.startswith('test_') or Path(path).name.endswith('_test.py'): facts['tests'].append({'file':path,'count':len(testnames),'names':sorted(testnames)})
        def function(node, owner=''):
            record={'name':node.name,'line':node.lineno,'decorators':[source(d) for d in node.decorator_list],'async':isinstance(node,ast.AsyncFunctionDef)}
            for dec in node.decorator_list:
                if not isinstance(dec,ast.Call) or not isinstance(dec.func,ast.Attribute) or dec.func.attr not in {'get','post','put','patch','delete','head','options','route','websocket','api_route'} or not dec.args or not isinstance(const(dec.args[0]),str): continue
                methods=const(kw(dec,'methods')) or ([dec.func.attr.upper()] if dec.func.attr not in {'route','api_route'} else ['GET'])
                if isinstance(methods,str): methods=[methods]
                for method in methods:
                    for prefix, resolved in prefixes(path, dec.func.value):
                        facts['routes'].append({'method':str(method).upper(),'path':const(dec.args[0]),'prefixed_path':prefix+const(dec.args[0]),'prefix_resolved':resolved,'handler':module+':'+(owner+'.' if owner else '')+node.name,'location':loc(path,node)})
            body=node.body
            if len(body)==1 and (isinstance(body[0],ast.Pass) or isinstance(body[0],ast.Expr) and isinstance(body[0].value,ast.Constant) and body[0].value.value is Ellipsis): markers.append({'kind':'STUB','location':loc(path,node),'text':source(body[0])[:160],'enclosing':(owner+'.' if owner else '')+node.name})
            return record
        functions={}; scopes=[]
        def collect_functions(node, owner=''):
            for child in ast.iter_child_nodes(node):
                if isinstance(child,(ast.FunctionDef,ast.AsyncFunctionDef)):
                    qualified=(owner+'.' if owner else '')+child.name
                    scopes.append((min([child.lineno]+[d.lineno for d in child.decorator_list]),
                                   child.end_lineno,qualified))
                    functions[id(child)]=function(child,owner)
                    collect_functions(child,qualified)
                elif isinstance(child,ast.ClassDef):
                    qualified=(owner+'.' if owner else '')+child.name
                    scopes.append((min([child.lineno]+[d.lineno for d in child.decorator_list]),
                                   child.end_lineno,qualified))
                    collect_functions(child,qualified)
                else:
                    collect_functions(child,owner)
        # Routes and stubs apply to every function; visibility only limits the
        # public module map below.
        collect_functions(tree)
        for node in tree.body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and not node.name.startswith('_'): item['functions'].append(functions[id(node)])
            elif isinstance(node,ast.ClassDef):
                bases=[source(b) for b in node.bases]; methods=[functions[id(n)] for n in node.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and not n.name.startswith('_')]
                if not node.name.startswith('_'): item['classes'].append({'name':node.name,'bases':bases,'decorators':[source(d) for d in node.decorator_list],'line':node.lineno,'docstring':first_doc(node),'methods':methods})
                table=None; columns=[]; fields=[]
                for n in node.body:
                    if isinstance(n,(ast.Assign,ast.AnnAssign)):
                        targets=n.targets if isinstance(n,ast.Assign) else [n.target]; val=n.value
                        for target in targets:
                            if not isinstance(target,ast.Name): continue
                            if target.id=='__tablename__': table=const(val)
                            if isinstance(val,ast.Call) and name(val.func) in {'Column','mapped_column'} or isinstance(n,ast.AnnAssign) and source(n.annotation).startswith('Mapped['): columns.append(target.id)
                            if not target.id.startswith('_'): fields.append(target.id)
                sqlmodel_table = any('SQLModel' in b for b in bases) and any(k.arg=='table' and const(k.value) is True for k in node.keywords)
                # Settings have their own facts group; SQLModel-derived bases
                # require table=True even when their names also contain Base.
                orm_base = any('Base' in b and 'BaseSettings' not in b and 'SQLModel' not in b for b in bases)
                if table or orm_base or sqlmodel_table: facts['models'].append({'class':node.name,'table':table,'columns':columns,'location':loc(path,node)})
                if any('BaseSettings' in b for b in bases): facts['settings'].append({'class':node.name,'fields':fields,'location':loc(path,node)})
        facts['modules'].append(item)
        if any(path.startswith(root + '/') for root in revision_roots):
            values={}
            for node in tree.body:
                if isinstance(node,(ast.Assign,ast.AnnAssign)):
                    targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target,ast.Name) and target.id in {'revision','down_revision'}: values[target.id]=const(node.value)
            if 'revision' in values:
                facts['migrations'].append({'revision':values.get('revision'),'down_revision':values.get('down_revision'),'message':first_doc(tree),'file':path})
        for marker in markers:
            if marker['location'].rsplit(':',1)[0]!=path or marker['enclosing']: continue
            line=int(marker['location'].rsplit(':',1)[1])
            candidates=[scope for scope in scopes if scope[0]<=line<=scope[1]]
            if candidates:
                marker['enclosing']=max(candidates,key=lambda scope:(scope[2].count('.'),scope[0]))[2]
    children=Counter()
    for m in facts['migrations']:
        parents=m['down_revision'] if isinstance(m['down_revision'],(tuple,list)) else [m['down_revision']]
        children.update(p for p in parents if p)
    facts['migration_branches']=any(n>1 for n in children.values())
    remaining=list(facts['migrations']); known=set()
    while remaining:
        ready=[m for m in remaining if not m['down_revision'] or all(p in known for p in (m['down_revision'] if isinstance(m['down_revision'],(tuple,list)) else [m['down_revision']]))]
        if not ready: ready=[remaining[0]]
        for m in ready: facts['migration_chain'].append(m['revision']); known.add(m['revision']); remaining.remove(m)
    return facts,markers,errors
def route_norm(value): return re.sub(r'\{[^}]+\}|<[^>]+>|:[A-Za-z_][\w]*','{}',value).rstrip('/') or '/'

def check_one(claim, facts):
    kind=claim.get('kind',''); value=str(claim.get('value','')); needle=value.casefold(); hits=[]; names={}
    def add(candidate, location, exact=False):
        if (candidate == value if exact else str(candidate).casefold()==needle): hits.append(location)
    if kind=='path': hits=[p for p in facts['files'] if fnmatch.fnmatchcase(p.casefold(),needle)]
    elif kind=='symbol':
        for mod in facts['modules']:
            for item in mod['functions']+mod['classes']:
                location=f"{mod['path']}:{item['line']}"; names[item['name']]=location
                for candidate in (item['name'],mod['module']+'.'+item['name']): add(candidate,location,True)
                for method in item.get('methods',[]):
                    location=f"{mod['path']}:{method['line']}"; names[method['name']]=location
                    for candidate in (method['name'],item['name']+'.'+method['name'],mod['module']+'.'+item['name']+'.'+method['name']): add(candidate,location,True)
    elif kind=='route':
        parts=value.split(None,1); method,path=(parts[0].upper(),parts[1]) if len(parts)==2 else ('',parts[0] if parts else '')
        for route in facts['routes']:
            if (not method or route['method']==method) and route_norm(path).casefold() in {route_norm(route['path']).casefold(),route_norm(route['prefixed_path']).casefold()}: hits.append(route['location'])
    elif kind in {'env','table'}:
        entries=([(x['name'],x['location']) for x in facts['env']]+[(f,x['location']) for x in facts['settings'] for f in x['fields']]) if kind=='env' else [(v,x['location']) for x in facts['models'] for v in (x['class'],x['table']) if v]
        for n,location in entries: names[n]=location; add(n,location)
    elif kind=='migration':
        for m in facts['migrations']:
            if str(m['revision'] or '').casefold().startswith(needle) or needle in m['message'].casefold(): hits.append(m['file'])
    elif kind=='test':
        for t in facts['tests']:
            if any(needle in n.casefold() for n in t['names']): hits.append(t['file'])
    elif kind=='dep':
        for d in facts['dependencies']:
            if d['name'].lower().replace('_','-')==needle.replace('_','-'): hits.append(d['file'])
    elif kind=='tf':
        for t in facts['terraform']:
            if needle in {t['type'].casefold(),(t['type']+'.'+t['name']).casefold()}: hits.append(t['location'])
    elif kind=='text':
        repo=Path(facts['repo']); count=0
        for path in facts['files']:
            if Path(path).suffix.lower() not in {'.py','.tf','.toml','.txt','.ini','.sh','.ps1','.cmd','.js','.ts','.tsx','.jsx','.go','.rs','.java','.c','.h','.cpp','.yaml','.yml','.json'} or excluded(path,facts['exclude']) or asset_path(path): continue
            f=text_file(repo, path, facts['exclude'])
            if f is None: continue
            count+=1
            try:
                for n,line in enumerate(f.read_text(encoding='utf8',errors='replace').splitlines(),1):
                    if needle in line.casefold(): hits.append(f'{path}:{n}'); break
            except OSError: pass
            if count>=200 or len(hits)>=3: break
    result={'kind':kind,'value':value,'found':bool(hits),'locations':sorted(set(hits))[:3]}
    if not hits and kind in {'symbol','table','env'}:
        if kind=='symbol': near=difflib.get_close_matches(value,sorted(names),n=3,cutoff=.75)
        else:
            mapping={n.casefold():n for n in names}; near=[mapping[n] for n in difflib.get_close_matches(needle,sorted(mapping),n=3,cutoff=.75)]
        result['near']=near
    return result

def markdown(facts,markers,cap):
    git=facts['git']; sections=[]

    def section(title,key,items,*,count=None,limit=None,intro=''):
        # Each row carries the actual artifact/key that recovers it. Counts
        # describe facts, including rows hidden by the fixed detail limits.
        rows=[(text,'codefacts.json → '+key) for text in items]
        sections.append({'heading':f'\n## {title} ({len(rows) if count is None else count})\n',
                         'rows':rows,'limit':limit,'intro':intro})
        return rows

    possible=any(d['status'] != '??' and '?' in d['status'] for d in git['dirty'])
    dirty_label='dirty or possible files' if possible else 'dirty files'
    git_rows=section('Git snapshot','git',
        [f"Branch: {git['branch']} | Head: {git['head']} | Commits: {git['total_commits']} | Dates: {git['first_date']} to {git['last_date']}"],
        count=f"{len(git['dirty'])} {dirty_label}, {len(git['commits'])} recent commits",
        limit=1+len(git['dirty'])+15)
    git_rows.extend((f"{'Possible (asset contents not read)' if d['status'] != '??' and '?' in d['status'] else 'Dirty'}: {d['status']} {d['path']}",'codefacts.json → git.dirty') for d in git['dirty'])
    git_rows.extend((f"{c['date']} {c['sha']} {c['subject']}",'codefacts.json → git.commits') for c in git['commits'])
    section('Routes','routes',[f"{r['method']} {r['prefixed_path']} | {r['handler']} | {r['location']}" for r in facts['routes']])
    section('ORM tables','models',[f"{m['table'] or m['class']}: {', '.join(m['columns'])}" for m in facts['models']])
    settings_count=sum(len(x['fields']) for x in facts['settings'])
    env_rows=section('Env and settings','env',[x['name'] for x in facts['env']],
        count=f"{len(facts['env'])} env vars, {settings_count} settings fields")
    env_rows.extend((f"{x['class']}.{field}",'codefacts.json → settings') for x in facts['settings'] for field in x['fields'])
    section('Migrations','migration_chain',[str(x) for x in facts['migration_chain']])
    section('Modules','modules',[f"{m['path']} — {m['docstring']} — {len(m['classes'])} classes, {len(m['functions'])} functions" for m in facts['modules']])
    kinds=Counter(m['kind'] for m in markers)
    directories=Counter(m['location'].rsplit(':',1)[0].split('/')[0]
                        if '/' in m['location'].rsplit(':',1)[0] else '.' for m in markers)
    marker_intro=''
    if markers:
        marker_intro='- By kind: '+', '.join(f'{k}: {v}' for k,v in sorted(kinds.items()))+'\n'
        marker_intro+='- By top-level dir: '+', '.join(f'{k}: {v}' for k,v in sorted(directories.items()))+'\n'
    marker_rows=section('Markers','',[],count=len(markers),limit=40,intro=marker_intro)
    marker_rows.extend((f"{m['location']} {m['kind']} {m['text']}",'markers.jsonl') for m in markers)
    section('Tests','tests',[f"{t['file']}: {t['count']}" for t in facts['tests']],
        count=f"{sum(t['count'] for t in facts['tests'])} total in {len(facts['tests'])} files")
    section('Dependencies','dependencies',[d['specifier'] for d in facts['dependencies']])
    section('Terraform','terraform',[f"{t['kind']} {t['type']}.{t['name']} {t['location']}" for t in facts['terraform']])
    section('Templates and static','assets',[f"{a['path']} ({a['bytes']} bytes)" for a in facts['assets']])
    budget=max(0,int(cap*3.5)); out=''

    def notice(index,offset=0):
        counts=Counter(pointer for _,pointer in sections[index]['rows'][offset:])
        return ''.join(f'… {count} more ({pointer})\n' for pointer,count in counts.items())

    def tail(index):
        # Every section keeps its count heading, even when all its rows are
        # omitted. Reserve the notices before spending space on any detail.
        return ''.join(sections[i]['heading']+notice(i) for i in range(index,len(sections)))

    minimum=tail(0)
    if len(minimum)>budget:
        raise ValueError(f'--max-md-tokens is too small to report all omitted sections; '
                         f'at least {math.ceil(len(minimum)/3.5)} tokens are required for this scan')
    for index,section_data in enumerate(sections):
        heading=section_data['heading']
        intro=redact(section_data['intro']).replace('\r','')
        items=section_data['rows']
        following=tail(index+1)
        if len(out+heading+intro+notice(index)+following)>budget:
            return out+tail(index)
        out+=heading+intro
        for i,(item,_) in enumerate(items):
            if section_data['limit'] is not None and i>=section_data['limit']:
                out+=notice(index,i)
                break
            line='- '+redact(str(item)).replace('\n',' ')+'\n'
            remaining=notice(index,i+1)
            if len(out+line+remaining+following)>budget:
                # Lower-priority sections must not consume space after any
                # item is omitted, except for their reserved omission notices.
                return out+notice(index,i)+following
            out+=line
    return out

def main(argv=None):
    parser=argparse.ArgumentParser(prog='codeclaims'); sub=parser.add_subparsers(dest='command',required=True)
    s=sub.add_parser('scan'); s.add_argument('--repo'); s.add_argument('--out',required=True); s.add_argument('--exclude',action='append'); s.add_argument('--max-md-tokens',type=int); s.add_argument('--config')
    c=sub.add_parser('check'); c.add_argument('--facts',required=True); c.add_argument('--in',dest='input',required=True); c.add_argument('--out',required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=='scan':
            config={}
            if args.config:
                with open(args.config,'rb') as f: config=tomllib.load(f)
                config=config.get('codefacts',config)
            repo=Path(args.repo or config.get('repo','')).resolve(); patterns=list(config.get('exclude',[]))+(args.exclude or []); cap=args.max_md_tokens if args.max_md_tokens is not None else config.get('max_md_tokens',15000)
            if not (args.repo or config.get('repo')) or not repo.is_dir() or cap<1: parser.error('a valid --repo and positive token cap are required')
            repo=require_source_root(repo,parser)
            out=output_path(args.out,repo,parser)
            # Validate every artifact before scanning or creating any output.
            paths={filename:output_path(out/filename,repo,parser) for filename in ('codefacts.json','codefacts.md','markers.jsonl','errors.jsonl')}
            facts,markers,errors=scan(repo,patterns)
            try: md=markdown(facts,markers,cap)
            except ValueError as exc: parser.error(str(exc))
            out.mkdir(parents=True,exist_ok=True)
            paths['codefacts.json'].write_text(dump(facts)+'\n',encoding='utf8')
            paths['codefacts.md'].write_text(md,encoding='utf8')
            paths['markers.jsonl'].write_text(''.join(dump(m)+'\n' for m in markers),encoding='utf8')
            paths['errors.jsonl'].write_text(''.join(dump(e)+'\n' for e in errors),encoding='utf8')
            print(f"Scanned {len(facts['files'])} files; {len(errors)} errors.")
        else:
            facts_path=Path(args.facts)
            if not facts_path.is_file(): parser.error('facts file does not exist')
            facts=json.loads(facts_path.read_text(encoding='utf8')); results=[]
            if not isinstance(facts.get('repo'),str) or not Path(facts['repo']).is_absolute():
                parser.error('facts must identify an absolute source repository path')
            if '[REDACTED:' in facts['repo'] or redact(facts['repo']) != facts['repo']:
                parser.error('facts source repository path is redacted; rescan from a path without sensitive identifiers')
            repo=require_source_root(facts['repo'],parser)
            out=output_path(args.out,repo,parser)
            for line in Path(args.input).read_text(encoding='utf8').splitlines():
                if not line.strip(): continue
                item=json.loads(line); claims=[check_one(x,facts) for x in item.get('claims',[])]; found=sum(x['found'] for x in claims)
                results.append({'id':item.get('id'),'verdict':'n/a' if not claims else 'verified' if found==len(claims) else 'partial' if found else 'not-found','claims':claims})
            out.write_text(''.join(dump(x)+'\n' for x in results),encoding='utf8'); print(f'Checked {len(results)} records.')
        return 0
    except (OSError,ValueError,KeyError,TypeError) as exc:
        print(redact(str(exc)),file=sys.stderr); return 3
