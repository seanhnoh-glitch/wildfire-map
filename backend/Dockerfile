# Wildfire Map backend — with ForeFire support
#
# This image compiles the ForeFire C++ engine and its Python bindings from
# source so the /predict endpoint can use real front-tracking physics.
# It is the recommended way to run the backend on Windows (via Docker Desktop).
#
# Build & run:
#   docker build -t wildfire-map-backend .
#   docker run -p 8000:8000 --env-file .env wildfire-map-backend
#
# Then open http://localhost:8000 in any browser.
#
# ForeFire is the only prediction engine: pyforefire is compiled into this image
# so /predict runs the Rothermel front-tracking simulation. If the simulation
# fails, /predict returns HTTP 503 (there is no elliptical-model fallback).
#
# Optional env vars (pass via --env-file .env or -e):
#   FIRMS_MAP_KEY=<free key from firms.modaps.eosdis.nasa.gov>
#   FOREFIRE_PROPAGATION_MODEL=Rothermel   # must match the extended fuel table

FROM ubuntu:22.04

ENV DEBIAN_FRONTEND=noninteractive

# --- System dependencies for ForeFire build ---
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        git \
        curl \
        libnetcdf-dev \
        libnetcdf-c++4-dev \
        python3 \
        python3-pip \
        python3-dev \
    && rm -rf /var/lib/apt/lists/*

# --- Build ForeFire C++ library and binary ---
RUN git clone --depth=1 https://github.com/forefireAPI/forefire.git /opt/forefire
WORKDIR /opt/forefire
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release \
    && cmake --build build --parallel $(nproc)

# Add the forefire binary to PATH (used for FOREFIRE_BINARY fallback path)
RUN ln -s /opt/forefire/build/forefire /usr/local/bin/forefire

# --- Install pyforefire Python bindings ---
# pyforefire is in bindings/python within the cloned repo.
# netCDF4 and numpy are required by pyforefire internally.
RUN pip3 install --no-cache-dir \
        numpy \
        netCDF4 \
        /opt/forefire/bindings/python

# --- Install the backend's own Python dependencies ---
WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

# --- Copy application source ---
COPY . .

ENV FOREFIRE_BINARY=/usr/local/bin/forefire

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
