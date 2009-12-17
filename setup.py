#!/usr/bin/env python

from setuptools import setup, find_packages

extra = {}

try:
    import babel
    extra['message_extractors'] = {
        'tracext': [
            ('**.py',                'python', None),
        ],
    }
except ImportError:
    pass
    
TracMercurial = 'http://trac.edgewall.org/wiki/TracMercurial'

setup(name='TracMercurial',
      install_requires='Trac >=0.12multirepos',
      description='Mercurial plugin for Trac multirepos branch',
      keywords='trac scm plugin mercurial hg',
      version='0.12.0.20',
      url=TracMercurial,
      license='GPL',
      author='Christian Boos',
      author_email='cboos@neuf.fr',
      long_description="""
      This plugin for Trac 0.12 provides support for the Mercurial SCM.

      '''The http://trac.edgewall.org/browser/sandbox/multirepos branch is
      required.'''
      
      See %s for more details.
      """ % TracMercurial,
      namespace_packages=['tracext'],
      packages=['tracext', 'tracext.hg'],
      package_data={
          '': ['COPYING', 'README'],
          'tracext.hg': ['locale/*.*', 'locale/*/LC_MESSAGES/*.*'],
          },
      entry_points={'trac.plugins': 'hg = tracext.hg.backend'},
      **extra)
