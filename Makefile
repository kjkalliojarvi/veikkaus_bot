.PHONY: clean clean-test clean-pyc clean-build docs help
.DEFAULT_GOAL := help
PIPFLAGS ?= --disable-pip-version-check --no-cache-dir
PYTHON_VERSION ?= python3.11

DEPS = $(shell git ls-files)

.PHONY: all
all: .built

.built: $(DEPS) .pipenv .pipdevenv
	touch $@

.pipenv: requirements.txt Makefile .venv/bin/activate
	. .venv/bin/activate && [ ! -s $< ] || $(PYTHON_VERSION) -m pip install $(PIPFLAGS) -r $<

.pipdevenv: requirements_dev.txt Makefile .venv/bin/activate
	. .venv/bin/activate && [ ! -s $< ] || $(PYTHON_VERSION) -m pip install $(PIPFLAGS) -r $<

.venv/bin/activate:
	[ -f .venv/bin/activate ] || ($(PYTHON_VERSION) -m venv --prompt veikkaus_bot .venv \
		&& . .venv/bin/activate && pip install --upgrade --force-reinstall pip setuptools)

.clean_venv:
	rm -rf .venv

clean: clean-build clean-pyc clean-test ## remove all build, test, coverage and Python artifacts

clean-build: ## remove build artifacts
	rm -fr build/
	rm -fr dist/
	rm -fr .eggs/
	find . -name '*.egg-info' -exec rm -fr {} +
	find . -name '*.egg' -exec rm -f {} +

clean-pyc: ## remove Python file artifacts
	find . -name '*.pyc' -exec rm -f {} +
	find . -name '*.pyo' -exec rm -f {} +
	find . -name '*~' -exec rm -f {} +
	find . -name '__pycache__' -exec rm -fr {} +

clean-test: ## remove test and coverage artifacts
	rm -fr .tox/
	rm -f .coverage
	rm -fr htmlcov/
	rm -fr .pytest_cache

.PHONY: run-tests
run-tests: .built
	. .venv/bin/activate && python -m pytest tests

coverage: ## check code coverage quickly with the default Python
	coverage run --source veikkaus_bot setup.py test
	coverage report -m
	coverage html
	$(BROWSER) htmlcov/index.html

docs: ## generate Sphinx HTML documentation, including API docs
	rm -f docs/veikkaus.rst
	rm -f docs/modules.rst
	sphinx-apidoc -o docs/ veikkaus_bot
	$(MAKE) -C docs clean
	$(MAKE) -C docs html
	$(BROWSER) docs/_build/html/index.html

servedocs: docs ## compile the docs watching for changes
	watchmedo shell-command -p '*.rst' -c '$(MAKE) -C docs html' -R -D .

release: dist ## package and upload a release
	twine upload dist/*

dist: clean ## builds source and wheel package
	python setup.py sdist
	python setup.py bdist_wheel
	ls -l dist

install: clean ## install the package to the active Python's site-packages
	python setup.py install
