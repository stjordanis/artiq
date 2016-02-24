import os, sys, tempfile, subprocess
from artiq.compiler import types
from llvmlite_artiq import ir as ll, binding as llvm

llvm.initialize()
llvm.initialize_all_targets()
llvm.initialize_all_asmprinters()

class RunTool:
    def __init__(self, pattern, **tempdata):
        self.files = []
        self.pattern = pattern
        self.tempdata = tempdata

    def maketemp(self, data):
        f = tempfile.NamedTemporaryFile()
        f.write(data)
        f.flush()
        self.files.append(f)
        return f

    def __enter__(self):
        tempfiles = {}
        tempnames = {}
        for key in self.tempdata:
            tempfiles[key] = self.maketemp(self.tempdata[key])
            tempnames[key] = tempfiles[key].name

        cmdline = []
        for argument in self.pattern:
            cmdline.append(argument.format(**tempnames))

        process = subprocess.Popen(cmdline, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise Exception("{} invocation failed: {}".
                            format(cmdline[0], stderr.decode('utf-8')))

        tempfiles["__stdout__"] = stdout.decode('utf-8')
        return tempfiles

    def __exit__(self, exc_typ, exc_value, exc_trace):
        for f in self.files:
            f.close()

def _dump(target, kind, suffix, content):
    if target is not None:
        print("====== {} DUMP ======".format(kind.upper()), file=sys.stderr)
        content_bytes = bytes(content(), 'utf-8')
        if target == "":
            file = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
        else:
            file = open(target + suffix, "wb")
        file.write(content_bytes)
        file.close()
        print("{} dumped as {}".format(kind, file.name), file=sys.stderr)

class Target:
    """
    A description of the target environment where the binaries
    generated by the ARTIQ compiler will be deployed.

    :var triple: (string)
        LLVM target triple, e.g. ``"or1k"``
    :var data_layout: (string)
        LLVM target data layout, e.g. ``"E-m:e-p:32:32-i64:32-f64:32-v64:32-v128:32-a:0:32-n32"``
    :var features: (list of string)
        LLVM target CPU features, e.g. ``["mul", "div", "ffl1"]``
    :var print_function: (string)
        Name of a formatted print functions (with the signature of ``printf``)
        provided by the target, e.g. ``"printf"``.
    """
    triple = "unknown"
    data_layout = ""
    features = []


    def __init__(self):
        self.llcontext = ll.Context()

    def compile(self, module):
        """Compile the module to a relocatable object for this target."""

        if os.getenv("ARTIQ_DUMP_SIG"):
            print("====== MODULE_SIGNATURE DUMP ======", file=sys.stderr)
            print(module, file=sys.stderr)

        type_printer = types.TypePrinter()
        _dump(os.getenv("ARTIQ_DUMP_IR"), "ARTIQ IR", ".txt",
              lambda: "\n".join(fn.as_entity(type_printer) for fn in module.artiq_ir))

        llmod = module.build_llvm_ir(self)

        try:
            llparsedmod = llvm.parse_assembly(str(llmod))
            llparsedmod.verify()
        except RuntimeError:
            _dump("", "LLVM IR (broken)", ".ll", lambda: str(llmod))
            raise

        _dump(os.getenv("ARTIQ_DUMP_UNOPT_LLVM"), "LLVM IR (generated)", "_unopt.ll",
              lambda: str(llparsedmod))

        llpassmgrbuilder = llvm.create_pass_manager_builder()
        llpassmgrbuilder.opt_level  = 2 # -O2
        llpassmgrbuilder.size_level = 1 # -Os
        llpassmgrbuilder.inlining_threshold = 75 # -Os threshold

        llpassmgr = llvm.create_module_pass_manager()
        llpassmgrbuilder.populate(llpassmgr)
        llpassmgr.run(llparsedmod)

        _dump(os.getenv("ARTIQ_DUMP_LLVM"), "LLVM IR (optimized)", ".ll",
              lambda: str(llparsedmod))

        lltarget = llvm.Target.from_triple(self.triple)
        llmachine = lltarget.create_target_machine(
                        features=",".join(["+{}".format(f) for f in self.features]),
                        reloc="pic", codemodel="default")

        _dump(os.getenv("ARTIQ_DUMP_ASM"), "Assembly", ".s",
              lambda: llmachine.emit_assembly(llparsedmod))

        return llmachine.emit_object(llparsedmod)

    def link(self, objects, init_fn):
        """Link the relocatable objects into a shared library for this target."""
        with RunTool([self.triple + "-ld", "-shared", "--eh-frame-hdr", "-init", init_fn] +
                     ["{{obj{}}}".format(index) for index in range(len(objects))] +
                     ["-o", "{output}"],
                     output=b"",
                     **{"obj{}".format(index): obj for index, obj in enumerate(objects)}) \
                as results:
            library = results["output"].read()

            _dump(os.getenv("ARTIQ_DUMP_ELF"), "Shared library", ".so",
                  lambda: library)

            return library

    def compile_and_link(self, modules):
        return self.link([self.compile(module) for module in modules],
                         init_fn=modules[0].entry_point())

    def strip(self, library):
        with RunTool([self.triple + "-strip", "--strip-debug", "{library}", "-o", "{output}"],
                     library=library, output=b"") \
                as results:
            return results["output"].read()

    def symbolize(self, library, addresses):
        if addresses == []:
            return []

        # We got a list of return addresses, i.e. addresses of instructions
        # just after the call. Offset them back to get an address somewhere
        # inside the call instruction (or its delay slot), since that's what
        # the backtrace entry should point at.
        offset_addresses = [hex(addr - 1) for addr in addresses]
        with RunTool([self.triple + "-addr2line", "--addresses",  "--functions", "--inlines",
                      "--exe={library}"] + offset_addresses,
                     library=library) \
                as results:
            lines = iter(results["__stdout__"].rstrip().split("\n"))
            backtrace = []
            while True:
                try:
                    address_or_function = next(lines)
                except StopIteration:
                    break
                if address_or_function[:2] == "0x":
                    address  = int(address_or_function[2:], 16) + 1 # remove offset
                    function = next(lines)
                else:
                    address  = backtrace[-1][4] # inlined
                    function = address_or_function
                location = next(lines)

                filename, line = location.rsplit(":", 1)
                if filename == "??" or filename == "<synthesized>":
                    continue
                # can't get column out of addr2line D:
                backtrace.append((filename, int(line), -1, function, address))
            return backtrace

class NativeTarget(Target):
    def __init__(self):
        super().__init__()
        self.triple = llvm.get_default_triple()

class OR1KTarget(Target):
    triple = "or1k-linux"
    data_layout = "E-m:e-p:32:32-i64:32-f64:32-v64:32-v128:32-a:0:32-n32"
    features = ["mul", "div", "ffl1", "cmov", "addc"]
