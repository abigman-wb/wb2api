FROM python:3.12-slim

WORKDIR /app

# 依赖锁定：requirements.lock 钉死直接+传递依赖，避免每次重建漂到不同版本。
# requirements.txt 只保留可读的直接依赖；两者必须一起 COPY，缺任何一个 pip 都会报错。
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir -c requirements.lock -r requirements.txt && pip check

COPY converter.py responses_adapter.py responses_projection.py anthropic_adapter.py desensitize.py upstream_compat.py repository_instructions.py model_metadata.py wb_install.py wb_asar.py wb_envcheck.py turing_helper.js ./
COPY admin ./admin
COPY desktop/__init__.py desktop/common.py desktop/runtime.py desktop/updates.py desktop/version.json ./desktop/
COPY desktop/assets ./desktop/assets

EXPOSE 8790 8787

CMD ["python3", "-m", "uvicorn", "admin.server:app", "--host", "0.0.0.0", "--port", "8790"]
