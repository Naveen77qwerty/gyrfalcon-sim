PY ?= .venv/bin/python
NODE ?= node
DEMO := results/demo
SHOTS := results/shots
LIVE_PORT ?= 8811

.PHONY: help test test-fast plots demos demo-a demo-b demo-c demo-d dashboard live live-check shots check clean cc-swap

help:
	@echo "make test        run the full suite (needs node + chrome for the render check)"
	@echo "make test-fast   skip the browser checks"
	@echo "make plots       regenerate every experiment plot into results/"
	@echo "make demos       regenerate all four demo event logs"
	@echo "make demo-a      scenario A: 1% loss, GBN vs Falcon-style"
	@echo "make demo-b      scenario B: reordering and spurious retransmissions"
	@echo "make demo-c      scenario C: path failure and reroute"
	@echo "make demo-d      scenario D: incast fairness"
	@echo "make dashboard   serve the replay dashboard on http://localhost:8000"
	@echo "make live        serve live mode on http://localhost:8811 (chaos sliders active)"
	@echo "make shots       render every demo log in headless chrome and save a PNG"
	@echo "make live-check  start live mode, drive it in a browser, shut it down"
	@echo "make check       test + demos + render check + screenshots + live check"
	@echo ""
	@echo "demo_a.sh .. demo_d.sh do the same as the demo-* targets with a pointer to what to look at"

test:
	$(PY) -m pytest -q -p no:cacheprovider

test-fast:
	$(PY) -m pytest -q -p no:cacheprovider -m "not slow"

plots:
	@for f in loss_sweep reorder_sweep incast remote_disk multipath scheduler_policy cc_swap host_congestion; do \
		echo "--- $$f"; $(PY) experiments/$$f.py || exit 1; \
	done

demos:
	$(PY) scripts/demo_logs.py all

demo-a:
	$(PY) scripts/demo_logs.py a

demo-b:
	$(PY) scripts/demo_logs.py b

demo-c:
	$(PY) scripts/demo_logs.py c

demo-d:
	$(PY) scripts/demo_logs.py d

# The dashboard is plain static files; any server works, and it needs none.
dashboard:
	@echo "open http://localhost:8000/ and load logs from $(DEMO)/"
	@cd dashboard && $(PY) -m http.server 8000

# Live mode runs the simulation in-process and streams it, so it needs the web stack.
live:
	$(PY) -m gyrfalcon.apps.live --port $(LIVE_PORT)

live-check:
	@$(PY) -m gyrfalcon.apps.live --port $(LIVE_PORT) & echo $$! > /tmp/gyrfalcon-live.pid; \
	trap 'kill $$(cat /tmp/gyrfalcon-live.pid) 2>/dev/null || true' EXIT; \
	for i in $$(seq 1 60); do \
		$(PY) -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:$(LIVE_PORT)/api/health')" \
			>/dev/null 2>&1 && break; \
		sleep 0.5; \
	done; \
	$(NODE) scripts/live_check.js http://127.0.0.1:$(LIVE_PORT)

shots:
	@mkdir -p $(SHOTS)
	$(NODE) scripts/render_check.js $(DEMO)/*.jsonl --shots $(SHOTS)

cc-swap:
	$(PY) experiments/cc_swap.py

check: test demos shots live-check

clean:
	rm -rf results/*.png results/*.jsonl results/demo results/shots .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +


# Stretch: UDP/netem demo (requires root/tc on Linux)
netem-demo:
	@bash scripts/netem_demo.sh lo 1 5
