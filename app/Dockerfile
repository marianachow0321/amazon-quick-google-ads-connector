FROM python:3.14-slim

COPY --from=public.ecr.aws/awsguru/aws-lambda-adapter:1.0.1 /lambda-adapter /opt/extensions/lambda-adapter

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

WORKDIR /app

ARG GADS_MCP_REF=ba47210245f2925a130a2770a4d272d5dd0c91cd

RUN uv pip install --system \
      "google-ads-mcp @ https://github.com/googleads/google-ads-mcp/archive/${GADS_MCP_REF}.tar.gz" \
      "fastmcp>=3.2.0,<4" \
      "uvicorn>=0.30" \
      "boto3>=1.34"

COPY entrypoint.py .

ENV PORT=8080
EXPOSE 8080

CMD ["python", "entrypoint.py"]
