FROM public.ecr.aws/lambda/python:3.12@sha256:3856536f53e945debf4c90d0ff301cbaa837c07a4ab9b0e2c102c6e54ceb9762
# OpenCV's Linux wheel links to these runtime libraries, including in batch mode.
RUN dnf install -y mesa-libGL glib2 libgomp && dnf clean all
COPY pyproject.toml requirements.lock README.md LICENSE ${LAMBDA_TASK_ROOT}/
COPY shorts_qc ${LAMBDA_TASK_ROOT}/shorts_qc
COPY infra/handler.py ${LAMBDA_TASK_ROOT}/handler.py
RUN pip install --no-cache-dir -r requirements.lock && \
    pip install --no-cache-dir --no-deps --no-build-isolation . && \
    python -m shorts_qc.model --output /opt/models/text.onnx
ENV TEXT_MODEL=/opt/models/text.onnx
CMD ["handler.handler"]
