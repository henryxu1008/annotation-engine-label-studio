# Isolated API/RQ acceptance image. Supply an immutable Python 3.11 dependency
# image built from this repository's poetry.lock; never use a retained runtime.
ARG DEPENDENCY_IMAGE=annotation-engine-issue79-tests:20261021
FROM ${DEPENDENCY_IMAGE}
USER root
# COPY overlays keep files removed from the current source. Clear ONLY
# image-layer source roots (tracked instrumentation files remain current);
# dependencies remain in the separate venv. No host/runtime volume is mounted.
RUN rm -rf /label-studio/label_studio /workspace/label_studio
COPY label_studio /label-studio/label_studio
COPY pyproject.toml poetry.lock /label-studio/
WORKDIR /label-studio/label_studio
# The dependency image installs the package editable at /label-studio. Override
# that exact tree and remove its coverage-only PYTHONPATH; two source roots can
# otherwise mix current top-level Django apps with stale label_studio.* imports.
ENV PYTHONPATH=/label-studio:/label-studio/label_studio
RUN python -c "import tomllib; p=next(p for p in tomllib.load(open('/label-studio/poetry.lock','rb'))['package'] if p['name']=='rfc8785'); open('/tmp/jcs-requirements','w').write('rfc8785=='+p['version']+' --hash='+next(f['hash'] for f in p['files'] if f['file'].endswith('.whl')))" \
    && python -m pip install --no-deps --require-hashes -r /tmp/jcs-requirements \
    && python -c "import sys,label_studio; from pathlib import Path; assert sys.version_info[:2] == (3, 11); assert Path(label_studio.__file__).resolve() == Path('/label-studio/label_studio/__init__.py')" \
    && python core/version.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "manage.py"]
