init:
	pip install pipenv
	pipenv install --dev

test:
	pipenv run py.test

test-coverage:
	pipenv run py.test --cov-config .coveragerc --cov=autoclasswrapper

compile:
	pipenv run python setup.py sdist

upload-to-pypi: compile
	pipenv run twine upload dist/*
	# clean compiled
	rm -f dist/*.tar.gz
