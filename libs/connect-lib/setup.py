"""
Setup configuration for connect-lib
"""

from setuptools import setup, find_packages

setup(
    name="connect-lib",
    version="0.1.0",
    author="RevBridge",
    description="Shared library of RevBridge Connect: authentication, API keys, events",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
    ],
    python_requires=">=3.8",
    install_requires=[
        # Dependencies shared with the services use compatible RANGES (not ==), so that
        # connect-lib does not dictate exact versions and block dependency bumps in the
        # consumers (ResolutionImpossible conflicts). Exception: the OTel stack is pinned
        # (==) because api/sdk/exporter must move together, consistently with
        # instrumentation/semconv (independent ranges would let the resolver pick an
        # incompatible combination).
        "PyJWT>=2.12.1,<3",
        "fastapi>=0.139,<0.140",
        "passlib[bcrypt]>=1.7.4,<2",
        "bcrypt>=5,<6",
        "python-dotenv>=1.2,<2",
        "mysql-connector-python>=9.7,<10",
        "pydantic>=2.13.4,<3",
        # Redis
        "redis>=7.4,<8",
        # Cryptography (kept in this list so the lock file stays unchanged; the library
        # itself no longer imports it)
        "cryptography>=48,<50",
        # OpenTelemetry — PINNED on purpose (a consistent stack; the group is bumped here,
        # and consumers do not pin otel directly)
        "opentelemetry-api==1.43.0",
        "opentelemetry-sdk==1.43.0",
        "opentelemetry-semantic-conventions==0.64b0",
        "opentelemetry-exporter-otlp-proto-grpc==1.43.0",
        "opentelemetry-instrumentation-fastapi==0.64b0",
        "opentelemetry-instrumentation-logging==0.64b0",
        "opentelemetry-instrumentation-redis==0.64b0",
        "opentelemetry-instrumentation-httpx==0.64b0",
        "opentelemetry-instrumentation-mysql==0.64b0",
    ],
)
