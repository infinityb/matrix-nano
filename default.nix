# NIX_PATH=nixpkgs="https://github.com/NixOS/nixpkgs/archive/f4a33546bdb5f81bd6cceb1b3cb19667145fed83.tar.gz" \
#   nix-build -E 'with import <nixpkgs> {}; pkgs.python3.withPackages(p: [((pkgs.callPackage ./. {}) p)])'
#
#  nix-build --experimental-features 'nix-command flakes' -E 'with import <nixpkgs> {}; pkgs.callPackage ./. {}'
#
{ callPackage, fetchFromGitHub, lib, pkgs }:
let
  matrix-nio = callPackage ./matrix-nio.nix {
    version = "0.20.1";
    src = fetchFromGitHub {
      owner = "poljar";
      repo = "matrix-nio";
      rev = "37c781291e6d565c063e37fb8316107c4cad6ec7";
      sha256 = "sha256-6oMOfyl8yR8FMprPYD831eiXh9g/bqslvxDmVcrNK80=";
    };
  };
  nano = ps: ps.buildPythonPackage {
    pname = "nano";
    format = "pyproject";
    version = "1.0.0";
    src = ./packages/nano;
    doCheck = false;
    propagatedBuildInputs = [(matrix-nio ps)];
    nativeBuildInputs = [
      pkgs.git
      ps.poetry-core
    ];
  };
  python = pkgs.python3.withPackages(p: [(nano p)]);
  llama = (builtins.getFlake "github:ggerganov/llama.cpp?rev=b541b4f0b1d4d9871c831e47cd5ff661039d6101").packages.x86_64-linux.default;
  requiredCommands = [
    pkgs.ffmpeg_5
    pkgs.openai-whisper-cpp
    llama
  ];
  run = pkgs.runCommand "nano" {
    nativeBuildInputs = [
      pkgs.makeWrapper
      python
    ];
  } ''
    mkdir -p "$out/bin"
    cp ${./start-nano.py} "$out"/bin/start-nano
    chmod +x "$out"/bin/start-nano
    patchShebangs "$out/bin"
    wrapProgram "$out"/bin/start-nano \
      --prefix PATH : ${lib.makeBinPath requiredCommands}
  '';
in run