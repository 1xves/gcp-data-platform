FROM gcr.io/dataflow-templates-base/python310-template-launcher-base

ENV FLEX_TEMPLATE_PYTHON_PY_FILE="/dataflow/template/event_processor.py"

COPY . /dataflow/template

RUN apt-get update && apt-get install -y git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r /dataflow/template/requirements.txt
RUN pip install --no-cache-dir apache-beam[gcp]==2.53.0
RUN pip install --no-cache-dir /dataflow/template
