#!/usr/bin/env python

from setuptools import setup, find_packages

TracMercurial = 'http://trac.edgewall.org/wiki/TracMercurial'

setup(name='TracMercurial',
      install_requires=('Trac >=0.12dev'),
      description='Mercurial plugin for Trac 0.12',
      keywords='trac scm plugin mercurial hg',
      version='0.12.0.10',
      url=TracMercurial,
      license='GPL',
      author='Christian Boos',
      author_email='cboos@neuf.fr',
      long_description="""
      This plugin for Trac 0.12 (trunk) provides support for the Mercurial SCM.
      
      See %s for more details.
      """ % TracMercurial,
      namespace_packages=['tracext'],
      packages=['tracext', 'tracext.hg'],
      data_files=['COPYING', 'README'],
      entry_points={'trac.plugins': 'hg = tracext.hg.backend'})
