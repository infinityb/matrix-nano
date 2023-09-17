{ lib, pkgs, git, fetchFromGitHub, src, version }: ps: with ps; ps.buildPythonPackage rec {
  pname = "matrix-nio";
  format = "pyproject";
  inherit src version;

  postPatch = ''
    substituteInPlace pyproject.toml \
      --replace 'aiofiles = "^0.6.0"' 'aiofiles = "*"' \
      --replace 'h11 = "^0.12.0"' 'h11 = "*"' \
      --replace 'cachetools = { version = "^4.2.1", optional = true }' 'cachetools = { version = "*", optional = true }' \
      --replace 'aiohttp-socks = "^0.7.0"' 'aiohttp-socks = "*"'
  '';

  nativeBuildInputs = [
    git
    poetry-core
  ];

  propagatedBuildInputs = with pkgs.python3Packages; [
    aiofiles
    aiohttp
    aiohttp-socks
    attrs
    future
    h11
    h2
    jsonschema
    logbook
    pycryptodome
    unpaddedbase64
    atomicwrites
    cachetools
    python-olm
    peewee
    jinja2
  ];

  passthru.optional-dependencies = {
    e2e = with pkgs.python3Packages; [
      atomicwrites
      cachetools
      python-olm
      peewee
    ];
  };

  doCheck = false;

  nativeCheckInputs = with pkgs.python3Packages; [
    aioresponses
    faker
    hypothesis
    py
    pytest-aiohttp
    pytest-benchmark
    pytestCheckHook
  ] ++ passthru.optional-dependencies.e2e;

  pytestFlagsArray = [
    "--benchmark-disable"
  ];

  disabledTests = [
    # touches network
    "test_connect_wrapper"
    # time dependent and flaky
    "test_transfer_monitor_callbacks"
  ];

  meta = with lib; {
    homepage = "https://github.com/poljar/matrix-nio";
    changelog = "https://github.com/poljar/matrix-nio/blob/${version}/CHANGELOG.md";
    description = "A Python Matrix client library, designed according to sans I/O principles";
    # license = licenses.isc;
    # maintainers = with maintainers; [ tilpner emily symphorien ];
  };
}

