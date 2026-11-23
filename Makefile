# make            build dist/TriggerComplete.oxt (creates settings.json and SYSTEM.md from the examples if missing)
# make install    build and install into LibreOffice (close LibreOffice first)
# make uninstall  remove the extension from LibreOffice
# make test       unit tests
# make e2e        end-to-end test with Writer on Xvfb (needs soffice, Xvfb, xdotool)
# make mock       mock completion server on :8080 for manual testing

PYTHON ?= python3
CONFIG_DIR ?= $(CURDIR)
OXT := dist/TriggerComplete.oxt

.PHONY: build install uninstall test e2e mock clean

build: settings.json SYSTEM.md
	$(PYTHON) build.py --config-dir "$(CONFIG_DIR)" --out $(OXT)

settings.json:
	cp settings.example.json settings.json

SYSTEM.md:
	cp SYSTEM.example.md SYSTEM.md

install: build
	unopkg add --force $(OXT)

uninstall:
	unopkg remove org.triggercomplete

test:
	$(PYTHON) -m unittest discover -s tests

e2e:
	$(PYTHON) tests/e2e_libreoffice.py

mock:
	$(PYTHON) tools/mock_server.py --delay 0.1

clean:
	rm -rf dist
