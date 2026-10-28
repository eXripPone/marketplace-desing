"""Generate API handlers and DTOs from OpenAPI, then configure Decimal fields."""

from pathlib import Path
import subprocess
import sys

PROJECT_DIR = Path(__file__).resolve().parents[1]
GENERATOR_IMAGE = "openapitools/openapi-generator-cli:v7.26.0"
SPEC_PATH = "/local/openapi/marketplace.json"


def generate(*arguments):
    subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{PROJECT_DIR}:/local",
            GENERATOR_IMAGE,
            *arguments,
        ],
        check=True,
    )


def main():
    if not (PROJECT_DIR / "openapi/marketplace.json").is_file():
        raise SystemExit("OpenAPI file not found: openapi/marketplace.json")

    print("1/4: Validate OpenAPI", flush=True)
    generate("validate", "-i", SPEC_PATH)

    print("2/4: Generate FastAPI server", flush=True)
    generate(
        "generate", "-i", SPEC_PATH,
        "-g", "python-fastapi", "-o", "/local/generated",
        "--additional-properties", "fastapiImplementationPackage=app.api,useExternalImplementationPackage=true",
    )

    print("3/4: Generate models with Decimal", flush=True)
    generate(
        "generate", "-i", SPEC_PATH,
        "-g", "python", "-o", "/local/generated/src",
        "--global-property", "models",
        "--additional-properties", "packageName=openapi_server,mapNumberTo=Decimal",
    )

    print("4/4: Configure Decimal input conversion", flush=True)
    subprocess.run(
        [sys.executable, str(PROJECT_DIR / "tools/fix_decimal.py")],
        check=True,
    )
    print("Generation completed.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except FileNotFoundError as error:
        raise SystemExit(f"Required program not found: {error.filename}") from error
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Generation stopped: command exited with code {error.returncode}") from error
