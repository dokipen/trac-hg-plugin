# -*- coding: iso-8859-1 -*-
#
# Copyright (C) 2005 Edgewall Software
# Copyright (C) 2005-2006 Christian Boos <cboos@neuf.fr>
# All rights reserved.
#
# This software may be used and distributed according to the terms
# of the GNU General Public License, incorporated herein by reference.
#
# Author: Christian Boos <cboos@neuf.fr>

from datetime import datetime
import os
import time
import posixpath
import re

from genshi.builder import tag

from trac.core import *
from trac.config import _TRUE_VALUES as TRUE
from trac.util.compat import sorted, reversed
from trac.util.datefmt import utc
from trac.util.text import shorten_line, to_unicode
from trac.versioncontrol.api import Changeset, Node, Repository, \
                                    IRepositoryConnector, \
                                    NoSuchChangeset, NoSuchNode
from trac.wiki import IWikiSyntaxProvider

try:
    from mercurial import hg
    from mercurial.ui import ui
    from mercurial.repo import RepoError
    from mercurial.node import hex, short, nullid
    from mercurial.util import pathto
    try:
        # for most recent version (hg:chgset:731e739b8659 at 2006-11-15)
        from mercurial.cmdutil import walkchangerevs
    except ImportError:
        # for older version
        from mercurial.commands import walkchangerevs
    has_mercurial = True
except ImportError:
    has_mercurial = False
    ui = object
    pass

try:
    from trac.versioncontrol.web_ui import IPropertyRenderer
    has_property_renderer = True
except ImportError:
    has_property_renderer = False


### Components

if has_property_renderer:
    class CsetPropertyRenderer(Component):
        implements(IPropertyRenderer)

        def match_property(self, name, mode):
            return (name in ('Parents', 'Children', 'Tags') and
                    mode == 'revprop') and 4 or 0
        
        def render_property(self, name, mode, context, props):
            revs = props[name]
            def link(rev):
                chgset = context.env.get_repository().get_changeset(rev)
                return tag.a(rev, class_="changeset",
                             title=shorten_line(chgset.message),
                             href=context.href.changeset(rev))
            return tag([tag(link(rev), ', ') for rev in revs[:-1]],
                       link(revs[-1]))


class MercurialConnector(Component):

    implements(IRepositoryConnector, IWikiSyntaxProvider)

    def __init__(self):
        self._version = None

    def get_supported_types(self):
        """Support for `repository_type = hg`"""
        global has_mercurial
        if has_mercurial:
            yield ("hg", 8)

    def get_repository(self, type, dir, authname):
        """Return a `MercurialRepository`"""
        if not self._version:
            from mercurial.version import get_version
            self._version = get_version()
            self.env.systeminfo.append(('Mercurial', self._version))
        options = {}
        for key, val in self.config.options(type):
            options[key] = val
        return MercurialRepository(dir, self.log, options)


    # IWikiSyntaxProvider methods
    
    def get_wiki_syntax(self):
        return []

    def get_link_resolvers(self):
        yield ('cset', self._format_link)
        yield ('chgset', self._format_link)
        yield ('branch', self._format_link)    # go to the corresponding head
        yield ('tag', self._format_link)

    def _format_link(self, formatter, ns, rev, label):
        repos = self.env.get_repository()
        if ns == 'branch':
            for b, head in repos.get_branches(): ## FIXME
                if b == rev:
                    rev = head
                    break
        try:
            chgset = repos.get_changeset(rev)
            return tag.a(label, class_="changeset",
                         title=shorten_line(chgset.message),
                         href=formatter.href.changeset(rev))
        except NoSuchChangeset, e:
            return tag.a(label, class_="missing changeset",
                         title=to_unicode(e), rel="nofollow",
                         href=formatter.href.changeset(rev))

### Helpers 
        
class trac_ui(ui):
    def __init__(self):
        ui.__init__(self, interactive=False)
        
    def write(self, *args): pass
    def write_err(self, str): pass

    def readline(self):
        raise TracError('*** Mercurial ui.readline called ***')



### Version Control API
    
class MercurialRepository(Repository):
    """Repository implementation based on the mercurial API.

    This wraps a hg.repository object.
    The revision navigation follows the branches, and defaults
    to the first parent/child in case there are many.
    The eventual other parents/children are listed as
    additional changeset properties.
    """

    def __init__(self, path, log, options):
        self.ui = trac_ui()
        if isinstance(path, unicode):
            str_path = path.encode('utf-8')
            if not os.path.exists(str_path):
                str_path = path.encode('latin-1')
            path = str_path
        self.repo = hg.repository(ui=self.ui, path=path)
        self.path = self.repo.root
        self._show_rev = True
        if 'show_rev' in options and not options['show_rev'] in TRUE:
            self._show_rev = False
        self._node_fmt = 'node_format' in options \
                         and options['node_format']    # will default to 'short'
        if self.path is None:
            raise TracError(path + ' does not appear to ' \
                            'contain a Mercurial repository.')
        Repository.__init__(self, 'hg:%s' % path, None, log)

    def hg_time(self, timeinfo):
        # [hg b47f96a178a3] introduced an API change:
        if isinstance(timeinfo, tuple): # Mercurial 0.8 
            time = timeinfo[0]
        else:                           # Mercurial 0.7
            time = timeinfo.split()[0]
        return datetime.fromtimestamp(time, utc)

    def hg_node(self, rev):
        """Return a changelog node for the given revision.

        `rev` can be any kind of revision specification string.
        If `None`, ''tip'' will be returned.
        """
        try:
            if rev:
                rev = rev.split(':', 1)[0]
                if rev.isdigit():
                    try:
                        return self.repo.changelog.node(rev)
                    except:
                        pass
                return self.repo.lookup(rev)
            return self.repo.changelog.tip()
        except RepoError, e:
            raise NoSuchChangeset(rev)

    def hg_display(self, n):
        """Return user-readable revision information for node `n`.

        The specific format depends on the `node_format` and `show_rev` options
        """
        nodestr = self._node_fmt == "hex" and hex(n) or short(n)
        if self._show_rev:
            return '%s:%s' % (self.repo.changelog.rev(n), nodestr)
        else:
            return nodestr

    def close(self):
        self.repo = None

    def normalize_path(self, path):
        """Remove leading "/", except for the root"""
        return path and path.strip('/') or ''

    def normalize_rev(self, rev):
        """Return the changelog node for the specified rev"""
        return self.hg_display(self.hg_node(rev))

    def short_rev(self, rev):
        """Return the revision number for the specified rev, in compact form.
        """
        if rev:
            if isinstance(rev, basestring) and rev.isdigit():
                rev = int(rev)
            if 0 <= rev < self.repo.changelog.count():
                return rev # it was already a short rev
        return self.repo.changelog.rev(self.hg_node(rev))

    def get_quickjump_entries(self, rev):
        # branches
        if hasattr(self.repo, 'branchtags'):
            # New 0.9.2 style branches, since [hg 028fff46a4ac]
            for t, n in sorted(self.repo.branchtags().items(), reverse=True,
                               key=lambda (t, n): self.repo.changelog.rev(n)):
                yield ('branches', t, '/', self.hg_display(n))
        else:
            # Old style branches
            heads = self.repo.changelog.heads()
            brinfo = self.repo.branchlookup(heads)
            for head in heads:
                rev = self.hg_display(head)
                if head in brinfo:
                    branch = ' '.join(brinfo[head])
                else:
                    branch = rev
                yield ('(old-style) branches', branch, '/', rev)
        # tags
        for t, n in reversed(self.repo.tagslist()):
            try:
                yield ('tags', t, '/', self.hg_display(n))
            except KeyError:
                pass

    def get_changeset(self, rev):
        return MercurialChangeset(self, self.hg_node(unicode(rev)))

    def get_changesets(self, start, stop):
        """Follow each head and parents in order to get all changesets

        FIXME: this should be handled by the repository cache as well.
        
        The code below is only an heuristic, and doesn't work in the
        general case. E.g. look at the mercurial repository timeline
        for 2006-10-18, you need to give ''38'' daysback in order to see
        the changesets from 2006-10-17...
        This is because of the following '''linear''' sequence of csets:
          - 3445:233c733e4af5         10/18/2006 9:08:36 AM mpm
          - 3446:0b450267cf47         9/10/2006 3:25:06 AM  hopper
          - 3447:ef1032c223e7         9/10/2006 3:25:06 AM  hopper
          - 3448:6ca49c5fe268         9/10/2006 3:25:07 AM  hopper
          - 3449:c8686e3f0291         10/18/2006 9:14:26 AM hopper
          This is most probably because [3446:3448] correspond to
          old changesets that have been ''hg import''ed, with their
          original dates.
        """
        log = self.repo.changelog
        seen = {nullid: 1}
        seeds = log.heads()
        while seeds:
            cn = seeds[0]
            del seeds[0]
            time = self.hg_time(log.read(cn)[2])
            rev = log.rev(cn)
            if time < start:
                continue # assume no ancestor is younger and use next seed
                # (and that assumption is wrong for 3448 in the example above)
            elif time < stop:
                yield MercurialChangeset(self, cn)
            for p in log.parents(cn):
                if p not in seen:
                    seen[p] = 1
                    seeds.append(p)

    def get_node(self, path, rev=None):
        return MercurialNode(self, self.normalize_path(path), self.hg_node(rev))

    def get_oldest_rev(self):
        return self.hg_display(nullid)

    def get_youngest_rev(self):
        return self.hg_display(self.repo.changelog.tip())

    def previous_rev(self, rev):
        n = self.hg_node(rev)
        log = self.repo.changelog
        parents = [self.hg_display(p) for p in log.parents(n) if p != nullid]
        parents.sort()
        return parents and parents[0] or None
    
    def next_rev(self, rev, path=''): # NOTE: path ignored for now
        n = self.hg_node(rev)
        log = self.repo.changelog
        children = [self.hg_display(c) for c in log.children(n)]
        children.sort()
        return children and children[0] or None
    
    def rev_older_than(self, rev1, rev2):
        log = self.repo.changelog
        return log.rev(self.hg_node(rev1)) < log.rev(self.hg_node(rev2))

#    def get_path_history(self, path, rev=None, limit=None):
#         path = self.normalize_path(path)
#         rev = self.normalize_rev(rev)
#         expect_deletion = False
#         while rev:
#             if self.has_node(path, rev):
#                 if expect_deletion:
#                     # it was missing, now it's there again:
#                     #  rev+1 must be a delete
#                     yield path, rev+1, Changeset.DELETE
#                 newer = None # 'newer' is the previously seen history tuple
#                 older = None # 'older' is the currently examined history tuple
#                 for p, r in _get_history(path, 0, rev, limit):
#                     older = (p, r, Changeset.ADD)
#                     rev = self.previous_rev(r)
#                     if newer:
#                         if older[0] == path:
#                             # still on the path: 'newer' was an edit
#                             yield newer[0], newer[1], Changeset.EDIT
#                         else:
#                             # the path changed: 'newer' was a copy
#                             rev = self.previous_rev(newer[1])
#                             # restart before the copy op
#                             yield newer[0], newer[1], Changeset.COPY
#                             older = (older[0], older[1], 'unknown')
#                             break
#                     newer = older
#                 if older:
#                     # either a real ADD or the source of a COPY
#                     yield older
#             else:
#                 expect_deletion = True
#                 rev = self.previous_rev(rev)

    def sync(self):
        pass


class MercurialNode(Node):
    """A path in the repository, at a given revision.

    It encapsulates the repository manifest for the given revision.

    As directories are not first-class citizens in Mercurial,
    retrieving revision information for directory is much slower than
    for files.
    """

    def __init__(self, repos, path, n, manifest=None, mflags=None):
        self.repos = repos
        self.n = n
        log = repos.repo.changelog
        
        if not manifest:
            manifest_n = log.read(n)[0] # 0: manifest node
            manifest = repos.repo.manifest.read(manifest_n)
            if hasattr(repos.repo.manifest, 'readflags'):
                mflags = repos.repo.manifest.readflags(manifest_n)
        self.manifest = manifest
        self.mflags = mflags
        if isinstance(path, unicode):
            try:
                self._init_path(log, path.encode('utf-8'))
            except NoSuchNode:
                self._init_path(log, path.encode('latin-1'))
                # TODO: configurable charset for the repository, i.e. #3809
        else:
            self._init_path(log, path)

    def _init_path(self, log, path):
        kind = None
        if path in self.manifest: # then it's a file
            kind = Node.FILE
            self.file_n = self.manifest[path]
            self.file = self.repos.repo.file(path)
            log_rev = self.file.linkrev(self.file_n)
            node = log.node(log_rev)
        else: # it will be a directory if there are matching entries
            dir = path and path+'/' or ''
            entries = {}
            newest = -1
            for file in self.manifest.keys():
                if file.startswith(dir):
                    entry = file[len(dir):].split('/', 1)[0]
                    entries[entry] = 1
                    if path: # small optimization: we skip this for root node
                        file_n = self.manifest[file]
                        log_rev = self.repos.repo.file(file).linkrev(file_n)
                        newest = max(log_rev, newest)
            if entries:
                kind = Node.DIRECTORY
                self.entries = entries.keys()
                if newest >= 0:
                    node = log.node(newest)
                else: # ... as it's the youngest anyway
                    node = log.tip()
        if not kind:
            if log.tip() == nullid: # empty repository
                kind = Node.DIRECTORY
                self.entries = []
                node = nullid
            else:
                raise NoSuchNode(path, self.repos.hg_display(self.n))
        self.time = self.repos.hg_time(log.read(node)[2])
        rev = self.repos.hg_display(node)
        Node.__init__(self, path, rev, kind)
        self.created_path = path
        self.created_rev = rev
        self.data = None

    def get_content(self):
        if self.isdir:
            return None
        self.pos = 0 # reset the read()
        return self # something that can be `read()` ...

    def read(self, size=None):
        if self.isdir:
            return TracError("Can't read from directory %s" % self.path)
        if self.data is None:
            self.data = self.file.read(self.file_n)
            self.pos = 0
        if size:
            prev_pos = self.pos
            self.pos += size
            return self.data[prev_pos:self.pos]
        return self.data

    def get_entries(self):
        if self.isfile:
            return
        for entry in self.entries:
            if self.path:
                entry = posixpath.join(self.path, entry)
            yield MercurialNode(self.repos, entry, self.n,
                                self.manifest, self.mflags)

    def get_history(self, limit=None):
        newer = None # 'newer' is the previously seen history tuple
        older = None # 'older' is the currently examined history tuple
        log = self.repos.repo.changelog
        # directory history
        if self.isdir:
            if not self.path: # special case for the root
                for r in xrange(log.rev(self.n), -1, -1):
                    yield ('', self.repos.hg_display(log.node(r)),
                           r and Changeset.EDIT or Changeset.ADD)
                return
            # Code compatibility for ''walkchangerevs'':
            # In Mercurial 0.7, it had 5 arguments, but
            # [hg 1d7d0c07e8f3] removed the 3rd argument ('cwd').
            args = (self.repos.ui, self.repos.repo)
            if walkchangerevs.func_code.co_argcount == 5:
                args = args + (None,)
            args = args + (['path:%s' % self.path],
                           {'rev': ['%s:0' % hex(self.n)]})
            wcr = walkchangerevs(*args)
                         
            matches = {}
            for st, rev, fns in wcr[0]:
                if st == 'window':
                    matches.clear()
                elif st == 'add':
                    matches[rev] = 1
                elif st == 'iter':
                    if matches[rev]:
                        yield (self.path, self.repos.hg_display(log.node(rev)),
                               Changeset.EDIT)
            return
        # file history
        # FIXME: COPY currently unsupported        
        for file_rev in xrange(self.file.rev(self.file_n), -1, -1):
            rev = log.node(self.file.linkrev(self.file.node(file_rev)))
            older = (self.path, self.repos.hg_display(rev), Changeset.ADD)
            if newer:
                change = newer[0] == older[0] and Changeset.EDIT or \
                         Changeset.COPY
                newer = (newer[0], newer[1], change)
                yield newer
            newer = older
        if newer:
            yield newer

    def get_annotations(self):
        from mercurial.context import filectx
        fctx = filectx(self.repos.repo, self.path, self.n)
        annotations = []
        for fc, line in fctx.annotate(follow=True):
            annotations.append(fc.rev())
        return annotations
        
    def get_properties(self):
        if self.isfile:
            if self.mflags: # Mercurial upto 9.1
                exe = self.mflags[self.path]
            else: # assume Mercurial version >= [abd9a05fca0b]
                exe = self.manifest.execf(self.path)
            return exe and {'exe': '*'} or {}
        return {}
    # FIXME++: implement pset/pget/plist etc. in hg
    # (hm, extended changelog is about the *changelog*, not the manifest...)

    def get_content_length(self):
        if self.isdir:
            return None
        # since 441ea218414e, i.e. shortly after 0.8.1
        return self.file.size(self.file.rev(self.file_n))

    def get_content_type(self):
        if self.isdir:
            return None
        return ''

    def get_last_modified(self):
        return self.time


class MercurialChangeset(Changeset):
    """A changeset in the repository.

    This wraps the corresponding information from the changelog.
    The files changes are obtained by comparing the current manifest
    to the parent manifest(s).
    """
    
    def __init__(self, repos, n):
        log = repos.repo.changelog
        log_data = log.read(n)
        manifest, user, timeinfo, files, desc = log_data[:5]
        extra = {}
        if len(log_data) > 5: # extended changelog, since [hg 2f35961854fb]
            extra = log_data[5]
        time = repos.hg_time(timeinfo)
        Changeset.__init__(self, repos.hg_display(n), to_unicode(desc),
                           to_unicode(user), time)
        self.repos = repos
        self.n = n
        self.manifest_n = manifest
        self.files = files
        self.parents = [repos.hg_display(p) for p in log.parents(n) \
                        if p != nullid]
        self.children = [repos.hg_display(c) for c in log.children(n)]
        self.tags = [t for t in repos.repo.nodetags(n)]
        self.extra = extra

    if has_property_renderer:
        def get_properties(self):
            properties = {}
            if len(self.parents) > 1:
                properties['Parents'] = self.parents
            if len(self.children) > 1:
                properties['Children'] = self.children
            if len(self.tags):
                properties['Tags'] = self.tags
            return properties
    else: # remove once 0.11 is released
        def get_properties(self):
            def changeset_links(csets):
                return ' '.join(['[cset:%s]' % cset for cset in csets])
            if len(self.parents) > 1:
                yield ('Parents', changeset_links(self.parents), True, 'changeset')
            if len(self.children) > 1:
                yield ('Children', changeset_links(self.children), True, 'changeset')
            if len(self.tags):
                yield ('Tags', changeset_links(self.tags), True, 'changeset')
            for k, v in self.extra.iteritems():
                yield (k, v, False, 'message')

    def get_changes(self):
        repo = self.repos.repo
        log = repo.changelog
        parents = log.parents(self.n)
        manifest = repo.manifest.read(self.manifest_n)
        manifest1 = None
        if parents:
            man_node1 = log.read(parents[0])[0]
            manifest1 = repo.manifest.read(man_node1)
            manifest2 = None
            if len(parents) > 1:
                man_node2 = log.read(parents[1])[0]
                manifest2 = repo.manifest.read(man_node2)

        deletions = {}
        def detect_delete(pmanifest, p):
            for file in pmanifest.keys():
                if file not in manifest:
                    deletions[file] = p
        if manifest1:
            detect_delete(manifest1, self.parents[0])
        if manifest2:
            detect_delete(manifest2, self.parents[1])

        changes = []
        for file in self.files: # 'added' and 'edited' files
            if file in deletions: # and since Mercurial > 0.7 [hg c6ffedc4f11b]
                continue          # also 'deleted' files
            action = None
            # TODO: find a way to detect conflicts and show how they were solved
            if manifest1 and file in manifest1:
                action = Changeset.EDIT                
                changes.append((file, Node.FILE, action, file, self.parents[0]))
            if manifest2 and file in manifest2:
                action = Changeset.EDIT                
                changes.append((file, Node.FILE, action, file, self.parents[1]))
            if not action:
                rename_info = repo.file(file).renamed(manifest[file])
                if rename_info:
                    base_path = rename_info[0]
                    linkedrev = repo.file(base_path).linkrev(rename_info[1])
                    base_rev = self.repos.hg_display(log.node(linkedrev))
                    if base_path in deletions:
                        action = Changeset.MOVE
                        del deletions[base_path]
                    else:
                        action = Changeset.COPY
                else:
                    action = Changeset.ADD
                    base_path = ''
                    base_rev = None
                changes.append((file, Node.FILE, action, base_path, base_rev))

        for file, p in deletions.items():
            changes.append((file, Node.FILE, Changeset.DELETE, file, p))
        changes.sort()
        for change in changes:
            yield change

