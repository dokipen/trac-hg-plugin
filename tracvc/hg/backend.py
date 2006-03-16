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

from __future__ import generators

import os.path
import time
import posixpath
import re

from trac.util import TracError, shorten_line, escape
from trac.config import _TRUE_VALUES as TRUE
from trac.versioncontrol import Changeset, Node, Repository, \
                                IRepositoryConnector
from trac.versioncontrol.web_ui import ChangesetModule, BrowserModule
from trac.wiki import IWikiSyntaxProvider
from trac.core import *

try:
    from mercurial import hg
    from mercurial.ui import ui
    from mercurial.repo import RepoError
    from mercurial.node import hex, short, nullid
    from mercurial.util import pathto
    from mercurial.commands import walkchangerevs
    has_mercurial = True
except ImportError:
    has_mercurial = False
    ui = object
    pass


### Components

class MercurialConnector(Component):

    implements(IRepositoryConnector, IWikiSyntaxProvider)

    def get_supported_types(self):
        """Support for `repository_type = hg`"""
        global has_mercurial
        if has_mercurial:
            yield ("hg", 8)

    def get_repository(self, type, dir, authname):
        """Return a `MercurialRepository`"""
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
            for b, head in repos.get_branches():
                if b == rev:
                    rev = head
                    break
        try:
            chgset = repos.get_changeset(rev)
            return '<a class="changeset" title="%s" href="%s">%s</a>' \
                   % (escape(shorten_line(chgset.message)),
                      formatter.href.changeset(rev), label)
        except TracError, e:
            return '<a class="missing changeset" title="%s" href="%s"' \
                   ' rel="nofollow">%s</a>' \
                   % (str(e), formatter.href.changeset(rev), label)


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
        return int(time)

    def hg_node(self, rev):
        try:
            if rev:
                m = re.match(r"(\d+):", rev)
                if m:
                    rev = m.group(1)
                return self.repo.lookup(rev)
            return self.repo.changelog.tip()
        except RepoError, e:
            raise TracError(str(e))

    def hg_display(self, n):
        nodestr = self._node_fmt == "hex" and hex(n) or short(n)
        if self._show_rev:
            return '%s:%s' % (self.repo.changelog.rev(n), nodestr)
        else:
            return nodestr

    def normalize_path(self, path):
        """Remove leading "/", except for the root"""
        return path and path.strip('/') or ''

    def normalize_rev(self, rev):
        """Return the changelog node for the specified rev"""
        return self.hg_display(self.hg_node(rev))

    def short_rev(self, rev):
        """Return the revision number for the specified rev"""
        return self.repo.changelog.rev(self.hg_node(rev))

    def get_changeset(self, rev):
        return MercurialChangeset(self, self.hg_node(rev))

    def get_changesets(self, start, stop):
        """Follow each head and parents in order to get all changesets"""
        log = self.repo.changelog
        seen = {nullid: 1}
        seeds = log.heads()
        while seeds:
            cn = seeds[0]
            del seeds[0]
            time = self.hg_time(log.read(cn)[2])
            rev = log.rev(cn)
            if time < start:
                continue # assume no parent is younger and use next seed
            elif time < stop:
                yield MercurialChangeset(self, cn)
            for p in log.parents(cn):
                if p not in seen:
                    seen[p] = 1
                    seeds.append(p)

    def get_node(self, path, rev=None):
        return MercurialNode(self, self.normalize_path(path), self.hg_node(rev))

    def get_tags(self, rev):
        for tag, n in self.repo.tagslist():
            yield (tag, self.hg_display(n))

    def get_branches(self, rev):
        heads = self.repo.changelog.heads()
        brinfo = self.repo.branchlookup(heads)
        for head in heads:
            rev = self.hg_display(head)
            if head in brinfo:
                branch = ' '.join(brinfo[head])
            else:
                branch = rev
            yield (branch, rev)
            
    def get_oldest_rev(self):
        return self.hg_display(self.repo.changelog.node(0))

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
            mflags = repos.repo.manifest.readflags(manifest_n)
        self.manifest = manifest
        self.mflags = mflags
        
        kind = None
        if path in manifest: # then it's a file
            kind = Node.FILE
            file_n = manifest[path]
            log_rev = repos.repo.file(path).linkrev(file_n)
            node = log.node(log_rev)
        else: # it will be a directory if there are matching entries
            dir = path and path+'/' or ''
            entries = {}
            newest = -1
            for file in manifest.keys():
                if file.startswith(dir):
                    entry = file[len(dir):].split('/', 1)[0]
                    entries[entry] = 1
                    if path: # small optimization: we skip this for root node
                        file_n = manifest[file]
                        log_rev = repos.repo.file(file).linkrev(file_n)
                        newest = max(log_rev, newest)
            if entries:
                kind = Node.DIRECTORY
                self.entries = entries.keys()
                if newest >= 0:
                    node = log.node(newest)
                else: # ... as it's the youngest anyway
                    node = log.tip()
        if not kind:
            raise TracError("No node at %s in revision %s" \
                            % (path, repos.hg_display(n)))
        self.time = repos.hg_time(log.read(node)[2])
        rev = repos.hg_display(node)
        Node.__init__(self, path, rev, kind)
        self.created_path = path
        self.created_rev = rev

    def get_content(self):
        if self.isdir:
            return None
        return self # something that can be `read()` ...

    def read(self, size=None):
        if self.isdir:
            return TracError("Can't read from directory %s" % self.path)
        file_n = self.manifest[self.path]
        file = self.repos.repo.file(self.path)
        data = file.read(file_n)
        return size and data[:size] or data

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
        file_n = self.manifest[self.path]
        file = self.repos.repo.file(self.path)
        # FIXME: COPY currently unsupported        
        for file_rev in xrange(file.rev(file_n), -1, -1):
            rev = log.node(file.linkrev(file.node(file_rev)))
            older = (self.path, self.repos.hg_display(rev), Changeset.ADD)
            if newer:
                change = newer[0] == older[0] and Changeset.EDIT or \
                         Changeset.COPY
                newer = (newer[0], newer[1], change)
                yield newer
            newer = older
        if newer:
            yield newer

    def get_properties(self):
        if self.isfile:
            # WARNING: currently a bool, I suspect this will change...
            if self.mflags[self.path]: 
                return {'exe': '*'}
        return {}             # FIXME++: implement pset/pget/plist etc. in hg

    def get_content_length(self):
        if self.isdir:
            return None
        return len(self.read())

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
        manifest, user, timeinfo, files, desc = log.read(n)
        time = repos.hg_time(timeinfo)
        Changeset.__init__(self, repos.hg_display(n), desc, user, time)
        self.repos = repos
        self.n = n
        self.manifest_n = manifest
        self.files = files
        self.parents = [repos.hg_display(p) for p in log.parents(n) \
                        if p != nullid]
        self.children = [repos.hg_display(c) for c in log.children(n)]
        self.tags = [t for t in repos.repo.nodetags(n)]

    def get_properties(self):
        def changeset_links(csets):
            return ' '.join(['[cset:%s]' % cset for cset in csets])
        if len(self.parents) > 1:
            yield ('Parents', changeset_links(self.parents), True, 'changeset')
        if len(self.children) > 1:
            yield ('Children', changeset_links(self.children), True, 'changeset')
        if len(self.tags):
            yield ('Tags', changeset_links(self.tags), True, 'changeset')

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
        for file in self.files: # edited files
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

