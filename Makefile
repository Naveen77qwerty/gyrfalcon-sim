PY ?= .venv/bin/python
NODE ?= node
DEMO := results/demo

.PHONY: help test test-fast plots demos demo-a demo-b demo-c demo-d dashboard check clean

help:
	@echo "make test        run the full suite (needs node + chrome for the render check)"
	@echo "make test-fast   skip the browser render check"
	@echo "make plots       regenerate every experiment plot into results/"
	@echo "make demos       regenerate all four demo event logs"
	@echo "make demo-a      scenario A: 1% loss, GBN vs Falcon-style"
	@echo "make demo-b      scenario B: reordering and spurious retransmissions"
	@echo "make demo-c      scenario C: path failure and reroute"
	@echo "make demo-d      scenario D: incast fairness"
	@echo "make dashboard   serve the replay dashboard on http://localhost:8000"
	@echo "make check       test + demos + render check"

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

check: test demos
	$(NODE) scripts/render_check.js $(DEMO)/*.jsonl

clean:
	rm -rf results/*.png results/*.jsonl results/demo .pytest_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
