# -*- coding: iso-8859-1 -*-
#
# Copyright (C) 2005 Edgewall Software
# Copyright (C) 2005-2007 Christian Boos <cboos@neuf.fr>
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

import pkg_resources

from genshi.builder import tag

from trac.core import *
from trac.config import _TRUE_VALUES as TRUE
from trac.util.datefmt import utc
from trac.util.text import shorten_line, to_unicode
from trac.versioncontrol.api import Changeset, Node, Repository, \
                                    IRepositoryConnector, \
                                    NoSuchChangeset, NoSuchNode
from trac.versioncontrol.web_ui import IPropertyRenderer, RenderedProperty
from trac.wiki import IWikiSyntaxProvider

try:
    from trac.util.translation import domain_functions
    gettext, _, tag_, N_, add_domain = domain_functions('tracmercurial', 
    'gettext', '_', 'tag_', 'N_', 'add_domain')
except ImportError:
    from trac.util.translation import _, tag_, N_, gettext
    def add_domain(a,b): 
        pass

hg_import_error = []
try:
    # The new `demandimport` mechanism doesn't play well with code relying
    # on the `ImportError` exception being caught.
    # OTOH, we can't disable `demandimport` because mercurial relies on it
    # (circular reference issue). So for now, we activate `demandimport`
    # before loading mercurial modules, and desactivate it afterwards.
    #
    # See http://www.selenic.com/mercurial/bts/issue605
    
    try:
        from mercurial import demandimport
        demandimport.enable();
    except ImportError, hg_import_error:
        demandimport = None

    from mercurial import hg
    from mercurial.context import filectx
    from mercurial.ui import ui
    from mercurial.node import hex, short, nullid
    from mercurial.util import pathto, cachefunc
    from mercurial.cmdutil import walkchangerevs
    from mercurial import extensions
    from mercurial.extensions import loadall

    # Note: due to the nature of demandimport, there will be no actual 
    # import error until those symbols get accessed, so here we go:
    for sym in ("filectx ui hex short nullid pathto "
                "cachefunc walkchangerevs loadall".split()):
        if repr(globals()[sym]) == "<unloaded module '%s'>" % sym:
            hg_import_error.append(sym)
    if hg_import_error:
        hg_import_error = "Couldn't import symbols: "+','.join(hg_import_error)

    # Mercurial versions >= 1.2 won't have mercurial.repo.RepoError anymore
    from mercurial.repo import RepoError
    from mercurial.revlog import LookupError
    if repr(RepoError) == "<unloaded module 'RepoError'>":
        from mercurial.error import RepoError, LookupError
    
    if demandimport:
        demandimport.disable();
    
except ImportError, e:
    hg_import_error = e
    ui = object

### Components

class CsetPropertyRenderer(Component):
    implements(IPropertyRenderer)

    def match_property(self, name, mode):
        return (name in ('Parents', 'Children', 'Tags', 'Branch') and
                mode == 'revprop') and 4 or 0
    
    def render_property(self, name, mode, context, props):
        return RenderedProperty(name=gettext(name+':'), 
                name_attributes=[("class", "property")],
                content=self._render_property(name, mode, context, props))

    def _render_property(self, name, mode, context, props):
        repos, revs = props[name]
        def link(rev):
            chgset = repos.get_changeset(rev)
            return tag.a(rev, class_="changeset",
                         title=shorten_line(chgset.message),
                         href=context.href.changeset(rev, repos.reponame))
        if name == 'Parents' and len(revs) == 2: # merge
            new = context.resource.id[1]
            parent_links = [
                    (link(rev), ' (',
                     tag.a('diff', title=_("Diff against this parent "
                           "(show the changes merged from the other parents)"),
                           href=context.href.changeset(new, repos.reponame, 
                                                       old=rev)), ')')
                           for rev in revs]
            return tag([(parent, ', ') for parent in parent_links[:-1]],
                       parent_links[-1], tag.br(),
                       tag.span(tag_("Note: this is a %(merge)s changeset, "
                                     "the changes displayed below correspond "
                                     "to the merge itself.",
                                     merge=tag.strong('merge')),
                                class_='hint'), tag.br(), 
                       # TODO: only keep chunks present in both parents 
                       #       (conflicts) or in none (extra changes)
                       # tag.span('No changes means the merge was clean.',
                       #         class_='hint'), tag.br(), 
                       tag.span(tag_("Use the %(diff)s links above to see all "
                                     "the changes relative to each parent.",
                                     diff=tag.tt('(diff)')),
                                class_='hint'))
        return tag([tag(link(rev), ', ') for rev in revs[:-1]],
                   link(revs[-1]))

class HgExtPropertyRenderer(Component):
    implements(IPropertyRenderer)

    def match_property(self, name, mode):
       return name in ('transplant_source') and \
                mode == 'revprop' and 4 or 0
    
    def render_property(self, name, mode, context, props):
        repos, value = props[name]
        if name == 'transplant_source':
            try:
                chgset = MercurialChangeset(repos, value)
                link = tag.a(chgset.rev, class_="changeset",
                             title=shorten_line(chgset.message),
                             href=context.href.changeset(short(value),
                                                         repos.reponame))
            except LookupError:
                link = tag.a(hex(value), class_="missing changeset",
                             title=_("no such changeset"), rel="nofollow")
            return RenderedProperty(name=_("Transplant:"), content=link,
                                    name_attributes=[("class", "property")])

class HgDefaultPropertyRenderer(Component):
    implements(IPropertyRenderer)

    def match_property(self, name, mode):
       return mode == 'revprop' and 1 or 0
    
    def render_property(self, name, mode, context, props):
        repos, value = props[name]
        try:
            return unicode(value)
        except UnicodeDecodeError:
            if len(value) <= 100:
                return tag.tt(''.join(("%02x" % ord(c)) for c in value))
            else:
                return tag.em(_("(binary, size greater than 100 bytes)"))


class MercurialConnector(Component):

    implements(IRepositoryConnector, IWikiSyntaxProvider)

    def __init__(self):
        self._version = None
        self.ui = None
        locale_dir = pkg_resources.resource_filename(__name__, 'locale')
        add_domain(self.env.path, locale_dir)

    def _setup_ui(self, hgrc_path):
        self.ui = trac_ui(self.log)
        # (code below adapted from mercurial.dispatch._dispatch)

        # read the local repository .hgrc into a local ui object
        if hgrc_path:
            if not os.path.isabs(hgrc_path):
                hgrc_path = os.path.join(self.env.path, 'conf', hgrc_path)
            if not os.path.exists(hgrc_path):
                self.log.warn("[hg] hgrc file (%s) doesn't exist.", hgrc_path) 
            try:
                self.ui = trac_ui(self.log, parentui=self.ui)
                self.ui.check_trusted = False
                self.ui.readconfig(hgrc_path)
            except IOError, e:
                self.log.warn("[hg] hgrc file (%s) can't be read: %s", 
                              hgrc_path, e)

        extensions.loadall(self.ui)
        if hasattr(extensions, 'extensions'):
            for name, module in extensions.extensions():
                # setup extensions
                extsetup = getattr(module, 'extsetup', None)
                if extsetup:
                    extsetup()

    def get_supported_types(self):
        """Support for `repository_type = hg`"""
        global hg_import_error
        if hg_import_error:
            self.error = hg_import_error
            yield ("hg", -1)
        else:
            yield ("hg", 8)

    def get_repository(self, type, dir, repo_options):
        """Return a `MercurialRepository`"""
        if not self._version:
            try:
                from mercurial.version import get_version
                self._version = get_version()
            except ImportError: # gone in Mercurial 1.2 (hg:9626819b2e3d)
                from mercurial.util import version
                self._version = version()
            self.env.systeminfo.append(('Mercurial', self._version))
        options = {}
        for key, val in self.config.options(type):
            options[key] = val
        if isinstance(repo_options, basestring): # 0.11 compat
            repo_options = {'username': repo_options}
        options.update(repo_options)
        if not self.ui:
            self._setup_ui(options.get('hgrc'))
        return MercurialRepository(dir, self.log, self.ui, options)


    # IWikiSyntaxProvider methods
    
    def get_wiki_syntax(self):
        yield (r'[0-9a-f]{12,40}', lambda formatter, label, match:
               self._format_link(formatter, 'cset', label, label))

    def get_link_resolvers(self):
        yield ('cset', self._format_link)
        yield ('chgset', self._format_link)
        yield ('branch', self._format_link)    # go to the corresponding head
        yield ('tag', self._format_link)

    def _format_link(self, formatter, ns, rev, label):
        reponame = ''
        context = formatter.context
        while context:
            if context.resource.realm in ('source', 'changeset'):
                reponame = context.resource.id[0]
                break
            context = context.parent
        repos = self.env.get_repository(reponame)
        if repos:
            try:
                if ns == 'branch':
                    for b, n in repos.repo.branchtags().items():
                        if b == rev:
                            rev = repos.repo.changelog.rev(n)
                            break
                    else:
                        raise NoSuchChangeset(rev)
                chgset = repos.get_changeset(rev)
                return tag.a(label, class_="changeset",
                             title=shorten_line(chgset.message),
                             href=formatter.href.changeset(rev, reponame))
            except NoSuchChangeset, e:
                errmsg = to_unicode(e)
        else:
            errmsg = _("Repository '%(repo)s' not found", repo=reponame)
        return tag.a(label, class_="missing changeset",
                     title=errmsg, rel="nofollow")

### Helpers 
        
class trac_ui(ui):
    def __init__(self, log, *args, **kwargs):
        ui.__init__(self, *args)
        self.setconfig('ui', 'interactive', 'off')
        self.log = log
        
    def write(self, *args):
        for a in args:
            self.log.info('(mercurial status) %s', a)

    def write_err(self, *args):
        for a in args:
            self.log.warn('(mercurial warning) %s', a)

    def isatty(self): 
        return False

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

    def __init__(self, path, log, ui, options):
        self.ui = ui
        self.options = options
        self.reponame = None
        # TODO: per repository ui and options?
        if isinstance(path, unicode):
            str_path = path.encode('utf-8')
            if not os.path.exists(str_path):
                str_path = path.encode('latin-1')
            path = str_path
        try:
            self.repo = hg.repository(ui=ui, path=path)
            self.path = self.repo.root
        except RepoError, e:
            self.path = None
        self._show_rev = True
        if 'show_rev' in options and not options['show_rev'] in TRUE:
            self._show_rev = False
        self._node_fmt = 'node_format' in options \
                         and options['node_format']    # will default to 'short'
        if self.path is None:
            raise TracError(_("%(path)s does not appear to contain a Mercurial"
                              " repository.", path=path))
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
        except (LookupError, RepoError):
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
        if rev is not None: 
            rev = str(rev) 
        return self.hg_display(self.hg_node(rev)) 

    def short_rev(self, rev):
        """Return the revision number for the specified rev, in compact form.
        """
        if rev is not None:
            if isinstance(rev, basestring) and rev.isdigit():
                rev = int(rev)
            if hasattr(self.repo.changelog, 'count'):
                max_rev = self.repo.changelog.count() # 1.0.1 and below
            else:
                max_rev = len(self.repo) # after 1.0.1
            if 0 <= rev < max_rev:
                return rev # it was already a short rev
        return self.repo.changelog.rev(self.hg_node(rev))

    def get_quickjump_entries(self, rev):
        branches = {}
        tags = {}
        branchtags = self.repo.branchtags().items()
        tagslist = self.repo.tagslist()
        for b, n in branchtags:
            branches[n] = b
        for t, n in tagslist:
            tags.setdefault(n, []).append(t)
        def taginfo(n):
            if n in tags:
                return ' (%s)' % ', '.join(tags[n])
            else:
                return ''
        # branches
        for b, n in sorted(branchtags, reverse=True, 
                           key=lambda (b, n): self.repo.changelog.rev(n)):
            yield ('branches', b + taginfo(n), '/', self.hg_display(n))
        # heads
        for n in self.repo.heads():
            if n not in branches:
                h = self.hg_display(n)
                yield ('extra heads', h + taginfo(n), '/', h)
        # tags
        for t, n in reversed(tagslist):
            try:
                yield ('tags', ', '.join(tags.pop(n)), 
                       '/', self.hg_display(n))
            except KeyError:
                pass

    def get_path_url(self, path, rev):
        url = self.options.get('url')
        if url and (not path or path == '/'):
            if not rev:
                return url
            n = self.hg_node(rev)
            branch = self.repo[n].branch()
            if branch == 'default':
                return url
            return url + '#' + branch # URL for cloning that branch

            # Note: link to matching location in Mercurial's file browser 
            #rev = rev is not None and short(n) or 'tip'
            #return '/'.join([url, 'file', rev, path])
  
    def get_changeset(self, rev):
        return MercurialChangeset(self, self.hg_node(unicode(rev)))

    def get_changeset_uid(self, rev):
        return self.hg_node(rev)

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

    def previous_rev(self, rev): # path better ignored here
        n = self.hg_node(rev)
        log = self.repo.changelog
        for p in log.parents(n):
            return self.hg_display(p) # always follow first parent
    
    def next_rev(self, rev, path=''):
        n = self.hg_node(rev)
        if path and len(path): # might be a file
            fc = filectx(self.repo, path, n)
            if fc: # it is a file
                for child_fc in fc.children():
                    return self.hg_display(child_fc.node())
                else:
                    return None
            # it might be a directory (not supported for now) FIXME
        log = self.repo.changelog
        for c in log.children(n):
            return self.hg_display(c) # always follow first child
    
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

    def get_changes(self, old_path, old_rev, new_path, new_rev,
                    ignore_ancestry=1):
        """Generates changes corresponding to generalized diffs.
        
        Generator that yields change tuples (old_node, new_node, kind, change)
        for each node change between the two arbitrary (path,rev) pairs.

        The old_node is assumed to be None when the change is an ADD,
        the new_node is assumed to be None when the change is a DELETE.
        """
        old_node = new_node = None
        if self.has_node(old_path, old_rev):
            old_node = self.get_node(old_path, old_rev)
        else:
            raise NoSuchNode(old_path, old_rev, 'The Base for Diff is invalid')
        if self.has_node(new_path, new_rev):
            new_node = self.get_node(new_path, new_rev)
        else:
            raise NoSuchNode(new_path, new_rev, 'The Target for Diff is invalid')
        # check kind, both should be same.
        if new_node.kind != old_node.kind:
            raise TracError(
                _("Diff mismatch: "
                  "Base is a %(okind)s (%(opath)s in revision %(orev)s) "
                  "and Target is a %(nkind)s (%(npath)s in revision %(nrev)s).",
                  okind=old_node.kind, opath=old_path, orev=old_rev,
                  nkind=new_node.kind, npath=new_path, nrev=new_rev))
        # Correct change info from changelog(revlog)
        # Finding changes between two revs requires tracking back
        # several routes.
                              
        if new_node.isdir:
            # TODO: Should we follow rename and copy?
            # As temporary workaround, simply compare entry names.
            changes = []
            for path in new_node.manifest:
                if new_path and not path.startswith(new_path+'/'):
                    continue
                op = old_path + path[len(new_path):]
                if op not in old_node.manifest:
                    changes.append((path, None, new_node.subnode(path),
                                    Node.FILE, Changeset.ADD))
                elif old_node.manifest[op] != new_node.manifest[path]:
                    changes.append((path, old_node.subnode(op),
                                    new_node.subnode(path),
                                    Node.FILE, Changeset.EDIT))
                # otherwise, not changed
            for path in old_node.manifest:
                if old_path and not path.startswith(old_path+'/'):
                    continue
                np = new_path + path[len(old_path):]
                if np not in new_node.manifest:
                    changes.append((path, old_node.subnode(np), None,
                                    Node.FILE, Changeset.DELETE))
            for change in sorted(changes, key=lambda c: c[0]):
                yield(change[1], change[2], change[3], change[4])
        else:
            if old_node.manifest[old_node.path] != \
                   new_node.manifest[new_node.path]:
                yield(old_node, new_node, Node.FILE, Changeset.EDIT)
            

class MercurialNode(Node):
    """A path in the repository, at a given revision.

    It encapsulates the repository manifest for the given revision.

    As directories are not first-class citizens in Mercurial,
    retrieving revision information for directory is much slower than
    for files.
    """
    
    filectx = None

    def __init__(self, repos, path, n, manifest=None, dirnode=None):
        self.repos = repos
        self.n = n
        log = repos.repo.changelog
        
        if not manifest:
            manifest_n = log.read(n)[0] # 0: manifest node
            manifest = repos.repo.manifest.read(manifest_n)
        self.manifest = manifest
        self._dirnode = dirnode

        if isinstance(path, unicode):
            try:
                self._init_path(log, path.encode('utf-8'))
            except NoSuchNode:
                self._init_path(log, path.encode('latin-1'))
                # TODO: configurable charset for the repository, i.e. #3809
        else:
            self._init_path(log, path)

    def findnode(self, n_rev, dirnames):
        dirnodes = {}
        log = self.repos.repo.changelog
        for rev in xrange(n_rev, -1, -1):
            for f in self.repos.repo.changectx(rev).files():
                for d in dirnames[:]:
                    if f.startswith(d):
                        dirnodes[d] = log.node(rev)
                        dirnames.remove(d)
                        if not dirnames: # if nothing left to find
                            return dirnodes
        # FIXME if we get here, the manifest contained a file which was not found 
        #       in any changelog. This can't normally happen, but we may later on
        #       introduce a ''cut-off'' value for not going through the whole history
        #       and only return what we got so far
        return dirnodes

    def _init_path(self, log, path):
        kind = None
        if path in self.manifest: # then it's a file
            kind = Node.FILE
            self.filectx = self.repos.repo.filectx(path, self.n)
            log_rev = self.filectx.linkrev()
            node = log.node(log_rev)
        else: # it will be a directory if there are matching entries
            dir = path and path+'/' or ''
            entries = {}
            for file in self.manifest.keys():
                if file.startswith(dir):
                    entry = file[len(dir):].split('/', 1)[0]
                    entries[entry] = 1
            if entries:
                kind = Node.DIRECTORY
                self.entries = entries.keys()
                if path: # small optimization: we skip this for root node
                    if self._dirnode:
                        # big optimization, the node was precomputed in parent
                        node = self._dirnode
                    else:
                        # we find the most recent change for a file below dir
                        node = self.findnode(log.rev(self.n), [dir,] ).values()[0]
                else:
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
        self.node = node
        self.data = None

    def subnode(self, p, dirnode=None):
        """Return a node with the same revision information but another path"""
        return MercurialNode(self.repos, p, self.n, self.manifest, dirnode)

    def get_content(self):
        if self.isdir:
            return None
        self.pos = 0 # reset the read()
        return self # something that can be `read()` ...

    def read(self, size=None):
        if self.isdir:
            return TracError(_("Can't read from directory %(path)s", 
                               path=self.path))
        if self.data is None:
            self.data = self.filectx.data()
            self.pos = 0
        if size:
            prev_pos = self.pos
            self.pos += size
            return self.data[prev_pos:self.pos]
        return self.data

    def get_entries(self):
        if self.isfile:
            return
        
        log = self.repos.repo.changelog

        # dirnames are entries which are sub-directories
        dirnames = []
        for entry in self.entries:
            if self.path:
                entry = posixpath.join(self.path, entry)
            if not entry in self.manifest:
                dirnames.append(entry + '/')

        # pre-computing the node for each sub-directory
        if dirnames:
            n_rev = log.rev(self.n)
            node_rev = log.rev(self.node)
            dirnodes = self.findnode((n_rev > node_rev) and node_rev or n_rev, dirnames)
        else:
            dirnodes = {}

        for entry in self.entries:
            if self.path:
                entry = posixpath.join(self.path, entry)
            yield self.subnode(entry, dirnodes.get(entry+"/", None))

    def get_history(self, limit=None):
        newer = None # 'newer' is the previously seen history tuple
        older = None # 'older' is the currently examined history tuple
        repo = self.repos.repo
        log = repo.changelog
        
        # directory history
        if self.isdir:
            if not self.path: # special case for the root
                for r in xrange(log.rev(self.n), -1, -1):
                    yield ('', self.repos.hg_display(log.node(r)),
                           r and Changeset.EDIT or Changeset.ADD)
                return
            getchange = cachefunc(lambda r:repo.changectx(r).changeset())
            pats = ['path:' + self.path]
            opts = {'rev': ['%s:0' % hex(self.n)]}
            wcr = walkchangerevs(self.repos.ui, repo, pats, getchange, opts)
            for st, rev, fns in wcr[0]:
                if st == 'iter':
                    yield (self.path, self.repos.hg_display(log.node(rev)),
                           Changeset.EDIT)
            return
        # file history
        # FIXME: COPY currently unsupported        
        for file_rev in xrange(self.filectx.filerev(), -1, -1):
            file_node = self.filectx.filelog().node(file_rev)
            rev = log.node(self.filectx.filectx(file_node).linkrev())
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
        annotations = []
        if self.filectx:
            for fc, line in self.filectx.annotate(follow=True):
                annotations.append(fc.rev())
        return annotations
        
    def get_properties(self):
        if self.isfile and 'x' in self.manifest.flags(self.path):
            return {'exe': '*'}
        else:
            return {}

    def get_content_length(self):
        if self.isdir:
            return None
        return self.filectx.size()

    def get_content_type(self):
        if self.isdir:
            return None
        if 'mq' in self.repos.options:
            if self.path not in ('.hgignore', 'series'):
                return 'text/x-diff'
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
        self.branch = extra.pop("branch", None) 
        self.extra = extra

    hg_properties = [
        N_("Parents:"), N_("Children:"), N_("Branch:"), N_("Tags:")
    ]

    def get_properties(self):
        properties = {}
        if len(self.parents) > 1:
            properties['Parents'] = (self.repos, self.parents)
        if len(self.children) > 1:
            properties['Children'] = (self.repos, self.children)
        if self.branch:
            properties['Branch'] = (self.repos, [self.branch])
        if len(self.tags):
            properties['Tags'] = (self.repos, self.tags)
        for k, v in self.extra.iteritems():
            properties[k] = (self.repos, v)
        return properties

    def get_changes(self):
        repo = self.repos.repo
        log = repo.changelog
        parents = log.parents(self.n)
        manifest = repo.manifest.read(self.manifest_n)
        manifest1 = manifest2 = None
        if parents:
            man_node1 = log.read(parents[0])[0]
            manifest1 = repo.manifest.read(man_node1)
            if len(parents) > 1:
                man_node2 = log.read(parents[1])[0]
                manifest2 = repo.manifest.read(man_node2)

        deletions = {}
        def detect_delete(pmanifest, p):
            for f in pmanifest.keys():
                if f not in manifest:
                    deletions[f] = p
        if manifest1:
            detect_delete(manifest1, self.parents[0])
        if manifest2:
            detect_delete(manifest2, self.parents[1])

        renames = {}
        changes = []
        for f in self.files: # 'added' and 'edited' files
            if f in deletions: # and since Mercurial > 0.7 [hg c6ffedc4f11b]
                continue          # also 'deleted' files
            action = None
            # TODO: find a way to detect conflicts and show how they were solved
            if manifest1 and f in manifest1:
                action = Changeset.EDIT                
                changes.append((f, Node.FILE, action, f, self.parents[0]))
            if manifest2 and f in manifest2:
                action = Changeset.EDIT                
                changes.append((f, Node.FILE, action, f, self.parents[1]))

            if not action:
                rename_info = repo.file(f).renamed(manifest[f])
                if rename_info:
                    base_path, base_filenode = rename_info
                    base_ctx = repo.filectx(base_path, fileid=base_filenode)
                    base_rev = self.repos.hg_display(base_ctx.node())
                    if base_path in deletions:
                        action = Changeset.MOVE
                        renames[base_path] = f
                    else:
                        action = Changeset.COPY
                else:
                    action = Changeset.ADD
                    base_path = ''
                    base_rev = None
                changes.append((f, Node.FILE, action, base_path, base_rev))

        for f, p in deletions.items():
            if f not in renames:
                changes.append((f, Node.FILE, Changeset.DELETE, f, p))
        changes.sort()
        for change in changes:
            yield change

