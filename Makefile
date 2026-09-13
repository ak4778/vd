PROG ?= ./vvvv       # Program we are building
OUT ?= -o $(PROG)       # Compiler argument for output file
SOURCES = main.c mongoose.c net.c data_source.c   # Source code files
# Enable mongoose's built-in TLS backend (EC P-256 + AES/ChaCha20).
# mongoose.h only defaults to MG_TLS_BUILTIN under MG_ARCH_CUBE; on Win32/Linux
# the default is MG_TLS_NONE, so HTTPS listen sockets silently fail handshake
# with "TLS is not enabled". Define it explicitly here.
CFLAGS = -W -Wall -Wextra -g3 -ggdb -O3 -fno-omit-frame-pointer -I. -DMG_TLS=MG_TLS_BUILTIN -DMG_SOCK_LISTEN_BACKLOG_SIZE=1024 -DMG_IO_SIZE=65536    # Build options

# RSA CRT optimisation in mongoose's built-in TLS has a bug that causes
# "CRT signing failed" during the TLS 1.2 handshake with self-signed RSA
# certs on Windows. Disable it to fall back to standard modular exponentiation.
# (only affects RSA certs; EC certs bypass this code path entirely)
CFLAGS += -DMG_TLS_RSA_USE_CRT=0

# Custom log format: human-readable timestamp instead of hex milliseconds
CFLAGS += -DMG_ENABLE_CUSTOM_LOG=1

# Custom log line buffer (bytes, main.c s_lbuf).  2048 fits a full URI with a
# URL-encoded CJK keyword; oversized lines chunk-flush, nothing is lost.
CFLAGS += -DLOG_LINE_BUF_SIZE=2048

# Database mode: SQLite (default), CSV, or PostgreSQL
# To use SQLite (default): make
# To use CSV: make CSV_MODE=1
# To use PostgreSQL: make PG_MODE=1  (PG_HOME overrides the libpq install dir)
ifeq ($(CSV_MODE),1)
  CFLAGS += -DCSV_MODE
  SOURCES += csv_driver.c
  MODE = csv
else ifeq ($(PG_MODE),1)
  CFLAGS += -DPG_MODE
  SOURCES += pg_driver.c
  MODE = pg
  ifeq ($(OS),Windows_NT)
    # libpq dev files: default to the locally installed PostgreSQL (official
    # EDB install provides lib/libpq.a + bin/libpq.dll + include/).
    # Fallback (embedded PG from Python pgserver package):
    #   make PG_MODE=1 PG_HOME=C:/Users/<u>/AppData/Local/Programs/Python/Python311/Lib/site-packages/pgserver/pginstall
    PG_HOME ?= C:/Program Files/PostgreSQL/18
    # Quotes are required: "Program Files" contains a space, and unquoted
    # -I/-L paths would be split by gcc at the space.
    # Link dynamically against bin/libpq.dll (MinGW ld links a DLL directly).
    # Do NOT use lib/libpq.a: the EDB PG18 archive is a STATIC Meson build
    # (src/interfaces/libpq/libpq.a.p/*.obj) whose pg_encoding_* etc. symbols
    # live in companion archives libpgcommon_shlib.a/libpgport_shlib.a plus a
    # heap of system libs. bin first makes the unambiguous DLL choice.
    CFLAGS += -I"$(PG_HOME)/include" -L"$(PG_HOME)/bin" -L"$(PG_HOME)/lib" -lpq
    # vvvv.exe links the libpq import library -> libpq.dll (+ its OpenSSL/etc
    # runtime deps) must sit next to the exe at runtime.  PG_COPY_CMD copies
    # them AFTER linking in the $(PROG) recipe, so foreign DLLs never shadow
    # the toolchain's own libwinpthread/libiconv/libintl while gcc/collect2
    # run (CWD is first on the DLL search path for gcc's child processes).
  else
    # Linux: use system libpq (apt install libpq-dev / yum install libpq-devel):
    # headers in /usr/include, library found by ldconfig, so just -lpq.
    # For a non-system libpq (e.g. embedded pgserver), override PG_HOME.
    PG_HOME ?=
    ifneq ($(PG_HOME),)
      CFLAGS += -I$(PG_HOME)/include -L$(PG_HOME)/lib
    endif
    CFLAGS += -lpq
  endif
else
  SOURCES += sqlite_driver.c
  # sqlite3.c is a third-party amalgamation; compile separately with relaxed warnings
  SQLITE_OBJ = sqlite3.o
  MODE = sqlite
endif

ifeq ($(OS),Windows_NT)         # Windows settings. Assume MinGW compiler.
  PROG = vvvv.exe
  CC = gcc
  CFLAGS += -lws2_32
  # Force select() on Windows: mongoose global default sets MG_ENABLE_EPOLL=1
  # which fails to compile on MinGW (no <sys/epoll.h>).  MG_ENABLE_POLL=0 is
  # also required because MG_ENABLE_POLL=1 on MinGW hits known WSAPoll hangs.
  CFLAGS += -DMG_ENABLE_EPOLL=0 -DMG_ENABLE_POLL=0
  # WinSock2 fd_set tracks at most FD_SETSIZE sockets *simultaneously*
  # (count-based append, NOT handle-value indexed like Linux).  Above the
  # limit the *oldest* sockets are silently dropped from select() -- and
  # since new connections sit at the head of mgr->conns, the LISTEN socket
  # (tail) is dropped first -> server silently stops accepting new
  # connections.  Default 64 broke at ~78 concurrent connections.
  CFLAGS += -DFD_SETSIZE=4096
  MODE_STAMP = .mode_$(MODE).win
  RM_MODE = -del /Q /F .mode_csv.win .mode_sqlite.win .mode_pg.win 2>nul
  TOUCH_MODE = type nul >
  DEL_CMD = cmd /C del /Q /F /S
  # libpq.dll alone is NOT enough: official EDB builds link OpenSSL etc.
  # dynamically (STATUS_DLL_NOT_FOUND 0xC0000135 on launch). Copy the known
  # runtime deps alongside; "if exist" keeps it tolerant of version changes
  # (PG 16-18 all use these names; the list was derived by parsing libpq.dll's
  # PE import table). The embedded pgserver libpq only needed system DLLs, so
  # extra copies are harmless there.
  PG_RUNTIME_DLLS = libpq.dll libssl-3-x64.dll libcrypto-3-x64.dll libintl-9.dll libiconv-2.dll libwinpthread-1.dll
  PG_COPY_CMD = cmd /C "for %f in ($(PG_RUNTIME_DLLS)) do @if exist "$(subst /,\,$(PG_HOME))\bin\%f" copy /Y "$(subst /,\,$(PG_HOME))\bin\%f" . >nul"
  CHECK_PROG = @if not exist $(subst /,\,$(PROG)) (echo *** BUILD FAILED *** & exit 1)
  # Delete the exe when the build MODE changes. MinGW make has coarse mtime
  # granularity: rapid PG->CSV->SQLite switches can leave the (newly touched)
  # mode stamp and the (old-mode) exe in the same second, so make considers
  # the exe up-to-date and silently keeps the OLD driver build. The mode-stamp
  # filename itself changes per mode, so its recipe always runs on switch —
  # deleting the stale exe there forces the link below to execute.
  # Also drops the PG runtime DLLs on switch (they only serve the pg build;
  # leaving them in the root can shadow the toolchain's own DLLs for gcc).
  # Wrap in cmd /C "...": GNU make feeds recipe lines to a temp BATCH file,
  # where a bare FOR loop needs %%d; the child cmd /c context keeps %d valid.
  DEL_MODE_PROG = cmd /C "if exist $(PROG) del /Q /F $(PROG) & for %d in ($(PG_RUNTIME_DLLS)) do @if exist %d del %d"
else
  CFLAGS += -lpthread -ldl      # Link against pthread and dl for SQLite on Linux
  # Linux: use epoll() for scalable I/O multiplexing.  mongoose's MG_ARCH_UNIX
  # block already sets MG_ENABLE_EPOLL=1 under __linux__, but we spell it out
  # explicitly so it also works on distros where __linux__ is defined later.
  ifeq ($(shell uname -s 2>/dev/null),Linux)
    CFLAGS += -DMG_ENABLE_EPOLL=1 -DMG_ENABLE_POLL=0
  endif
  MODE_STAMP = .mode_$(MODE)
  RM_MODE = @rm -f .mode_csv .mode_sqlite .mode_pg
  TOUCH_MODE = @touch
  DEL_CMD = rm -rf
  # Linux loads system libpq.so via ldconfig; no DLL copy needed
  PG_COPY_CMD =
  DEL_MODE_PROG = @rm -f $(PROG)
  # No foreign DLLs on Linux; gcc resolves its own libs from the toolchain dir
  PARK_CMD =
  UNPARK_CMD =
  CHECK_PROG = @test -f $(PROG) || { echo "*** BUILD FAILED ***"; exit 1; }
endif

all: $(PROG)
	$(RUN) $(PROG) $(ARGS)

# Compile sqlite3.c separately: suppress unused-parameter warnings in third-party code
sqlite3.o: sqlite3.c
	$(CC) -c sqlite3.c $(filter-out -lws2_32 -lpthread -ldl,$(CFLAGS)) -Wno-unused-parameter -o sqlite3.o

$(PROG): $(SOURCES) $(SQLITE_OBJ) $(MODE_STAMP)
	$(CC) $(SOURCES) $(SQLITE_OBJ) $(CFLAGS) $(CFLAGS_MONGOOSE) $(CFLAGS_EXTRA) $(OUT)
	$(CHECK_PROG)
# PG mode on Windows only: copy the libpq runtime DLLs AFTER a successful
# link (not as a prerequisite) so they never sit in the CWD while gcc's
# child processes are running — CWD is first on their DLL search path and
# libwinpthread/libiconv/libintl from PG would shadow the toolchain's own.
ifeq ($(PG_MODE),1)
ifeq ($(OS),Windows_NT)
	$(PG_COPY_CMD)
endif
endif

$(MODE_STAMP):
	$(RM_MODE)
	-$(DEL_MODE_PROG)
	$(TOUCH_MODE) $@
	@echo === Building in $(MODE) mode ===

web_root/bundle.js:
	curl -s https://npm.reversehttp.com/preact,preact/hooks,htm/preact,preact-router -o $@

clean:
	$(DEL_CMD) $(PROG) $(PACK) $(PG_RUNTIME_DLLS) *.o *.obj *.exe *.dSYM .mode_*
ifeq ($(OS),Windows_NT)
	cmd /C "if exist .gccpark rmdir /S /Q .gccpark"
endif
