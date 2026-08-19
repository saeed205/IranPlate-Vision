.PHONY: install run run-https serve up down smoke test clean

install:
	pip install -r requirements.txt

run:            ## development server (Flask, single process)
	python app.py

run-https:      ## development server over TLS, for phone camera access
	python run_https.py

serve:          ## production server
	waitress-serve --host=0.0.0.0 --port=5000 --threads=8 app:app

up:
	docker compose up --build

down:
	docker compose down

smoke:          ## hit every endpoint against a running server
	python scripts/smoke_test.py

test:           ## offline checks, no server or models needed
	python scripts/test_plates.py
	python scripts/test_camera_worker.py

clean:
	rm -rf __pycache__ scripts/__pycache__ .jscheck
