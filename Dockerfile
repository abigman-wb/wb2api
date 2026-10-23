FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY converter.py responses_adapter.py responses_projection.py anthropic_adapter.py desensitize.py upstream_compat.py repository_instructions.py model_metadata.py wb_install.py wb_asar.py wb_envcheck.py turing_helper.js ./
COPY admin ./admin
COPY desktop/__init__.py desktop/common.py desktop/runtime.py desktop/updates.py desktop/version.json ./desktop/
COPY desktop/assets ./desktop/assets

EXPOSE 8790 8787

CMD ["python3", "-m", "uvicorn", "admin.server:app", "--host", "0.0.0.0", "--port", "8790"]
