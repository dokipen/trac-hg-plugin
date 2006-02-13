#!/usr/bin/env python

from setuptools import setup, find_packages

TracMercurial = 'http://projects.edgewall.com/trac/wiki/TracMercurial',

setup(name='TracMercurial',
      description='Mercurial plugin for Trac',
      keywords='trac scm plugin mercurial hg',
      version='0.2',
      url=TracMercurial,
      license='GPL',
      author='Christian Boos',
      author_email='cboos@neuf.fr',
      long_description="""
      This Trac 0.9+ plugin provides support for the Mercurial SCM.
      
      It requires a special development version of Trac, which features
      pluggable SCM backend providers, see %s for more details.
      """ % TracMercurial,
      packages=['trac', 'trac.versioncontrol'],
      namespace_packages = ['trac', 'trac.versioncontrol'],
      data_files=['COPYING', 'README'],
      entry_points={'trac.plugins': 'hg = trac.versioncontrol.hg.backend'})
