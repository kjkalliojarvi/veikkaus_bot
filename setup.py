#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""The setup script."""

from setuptools import setup, find_packages

with open('README.rst') as readme_file:
    readme = readme_file.read()

with open('HISTORY.rst') as history_file:
    history = history_file.read()

requirements = [
    'pydantic'=='2.6.4',
    'requests'=='2.31.0',
    'sqlalchemy'=='2.0.29',
    'python-decouple'=='3.8'
]

setup_requirements = [ ]

test_requirements = [ ]

setup(
    author="kk",
    author_email='kari.kalliojarvi@kolumbus.fi',
    classifiers=[
        'Development Status :: 2 - Pre-Alpha',
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12'
    ],
    description="Veikkaus bot",
    install_requires=requirements,
    license="MIT license",
    long_description=readme + '\n\n' + history,
    include_package_data=True,
    keywords='Veikkaus bot',
    name='Veikkaus bot',
    packages=find_packages(include=['veikkaus_bot']),
    setup_requires=setup_requirements,
    test_suite='tests',
    tests_require=test_requirements,
    url='https://github.com/kjkalliojarvi/veikkaus_bot',
    version='0.0.1',
    zip_safe=False,
    entry_points={
        'console_scripts':[
            'veikka=veikkaus_bot.__main__:veikka'
        ]
    },
)
