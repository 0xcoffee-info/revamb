/// \file debughelper.cpp
/// \brief This file handles debugging information generation.

//
// This file is distributed under the MIT License. See LICENSE.md for details.
//

// Standard includes
#include <fstream>

// LLVM includes
#include "llvm/IR/AssemblyAnnotationWriter.h"
#include "llvm/IR/Instruction.h"
#include "llvm/IR/LLVMContext.h"
#include "llvm/IR/Module.h"
#include "llvm/Support/FormattedStream.h"
#include "llvm/Support/raw_os_ostream.h"

// Local includes
#include "debughelper.h"

using namespace llvm;

/// Boring code to get the text of the metadata with the specified kind
/// associated to the given instruction
static MDString *getMD(const Instruction *Instruction, unsigned Kind) {
  assert(Instruction != nullptr);

  Metadata *MD = Instruction->getMetadata(Kind);

  if (MD == nullptr)
    return nullptr;

  auto Node = dyn_cast<MDNode>(MD);

  assert(Node != nullptr);

  const MDOperand& Operand = Node->getOperand(0);

  Metadata *MDOperand = Operand.get();

  if (MDOperand == nullptr)
    return nullptr;

  auto *String = dyn_cast<MDString>(MDOperand);
  assert(String != nullptr);

  return String;
}

/// Writes the text contained in the metadata with the specified kind ID to the
/// output stream, unless that metadata is exactly the same as in the previous
/// instruction.
static void writeMetadataIfNew(const Instruction *TheInstruction,
                               unsigned MDKind,
                               formatted_raw_ostream &Output,
                               StringRef Prefix) {
  MDString *MD = getMD(TheInstruction, MDKind);
  if (MD != nullptr) {
    MDString *PrevMD = nullptr;

    do {
      if (TheInstruction == TheInstruction->getParent()->begin())
        TheInstruction = nullptr;
      else {
        TheInstruction = TheInstruction->getPrevNode();
        PrevMD = getMD(TheInstruction, MDKind);
      }
    } while (TheInstruction != nullptr && PrevMD == nullptr);

    if (TheInstruction == nullptr || PrevMD != MD)
      Output << Prefix << MD->getString();

  }
}

/// Add a module flag, if not already present, using name and value provided.
/// Used for creating the Dwarf compliant debug info.
static void addModuleFlag(Module *TheModule, StringRef Flag, uint32_t Value) {
  if (TheModule->getModuleFlag(Flag) == nullptr) {
    TheModule->addModuleFlag(Module::Warning, Flag, Value);
  }
}

DebugAnnotationWriter::DebugAnnotationWriter(LLVMContext& Context,
                                             bool DebugInfo) :
  Context(Context),
  DebugInfo(DebugInfo)
{
  OriginalInstrMDKind = Context.getMDKindID("oi");
  PTCInstrMDKind = Context.getMDKindID("pi");
  DbgMDKind = Context.getMDKindID("dbg");
}

void DebugAnnotationWriter::emitInstructionAnnot(const Instruction *Instr,
                                                 formatted_raw_ostream &Output) {
  DISubprogram *Subprogram = Instr->getParent()->getParent()->getSubprogram();

  // Ignore whatever is outside the root and the isolated functions
  if (Subprogram == nullptr)
    return;

  writeMetadataIfNew(Instr, OriginalInstrMDKind, Output, "\n  ; ");
  writeMetadataIfNew(Instr, PTCInstrMDKind, Output, "\n  ; ");

  if (DebugInfo) {
    // If DebugInfo is activated the generated LLVM IR textual representation
    // will contain some reference to dangling pointers. So ignore the output
    // stream if you're using the annotator to generate debug info about the IR
    // itself.
    assert(Subprogram != nullptr);

    // Flushing is required to have correct line and column numbers
    Output.flush();
    auto *Location = DILocation::get(Context,
                                     Output.getLine() + 1,
                                     Output.getColumn(),
                                     Subprogram);

    // Sorry Bjarne
    auto *NonConstInstruction = const_cast<Instruction *>(Instr);
    NonConstInstruction->setMetadata(DbgMDKind, Location);
  }
}

DebugHelper::DebugHelper(std::string Output,
                         std::string Debug,
                         Module *TheModule,
                         DebugInfoType Type) :
  OutputPath(Output),
  DebugPath(Debug),
  Builder(*TheModule),
  Type(Type),
  TheModule(TheModule)
{
  OriginalInstrMDKind = TheModule->getContext().getMDKindID("oi");
  PTCInstrMDKind = TheModule->getContext().getMDKindID("pi");
  DbgMDKind = TheModule->getContext().getMDKindID("dbg");

  // Generate automatically the name of the source file for debugging
  if (DebugPath.empty()) {
    if (Type == DebugInfoType::PTC)
      DebugPath = OutputPath + ".ptc";
    else if (Type == DebugInfoType::OriginalAssembly)
      DebugPath = OutputPath + ".S";
    else if (Type == DebugInfoType::LLVMIR)
      DebugPath = OutputPath;
  }

  if (Type != DebugInfoType::None) {
    CompileUnit = Builder.createCompileUnit(dwarf::DW_LANG_C,
                                            DebugPath,
                                            "",
                                            "revamb",
                                            false,
                                            "",
                                            0 /* Runtime version */);

    // Add the current debug info version into the module after checking if it
    // is already present.
    addModuleFlag(TheModule, "Debug Info Version", DEBUG_METADATA_VERSION);
    addModuleFlag(TheModule, "Dwarf Version", 4);
  }
}

void DebugHelper::generateDebugInfo() {
  for (Function &F : TheModule->functions()) {
    // TODO: find a better way to identify root and the isolated functions
    if (F.getName() == "root" || F.getName().startswith("bb.")) {
      if (Type != DebugInfoType::None) {
        DISubroutineType *EmptyType = nullptr;
        DITypeRefArray EmptyArrayType = Builder.getOrCreateTypeArray({});
        EmptyType = Builder.createSubroutineType(EmptyArrayType);

        assert(CompileUnit != nullptr);
        DISubprogram *Subprogram = nullptr;
        Subprogram = Builder.createFunction(CompileUnit->getFile(), // Scope
                                            F.getName(),
                                            StringRef(), // Linkage name
                                            CompileUnit->getFile(),
                                            1, // Line
                                            EmptyType, // Subroutine type
                                            false, // isLocalToUnit
                                            true, // isDefinition
                                            1, // ScopeLine
                                            DINode::FlagPrototyped,
                                            false /* isOptimized */);
        F.setSubprogram(Subprogram);
      }
    }
  }

  switch (Type) {
  case DebugInfoType::PTC:
  case DebugInfoType::OriginalAssembly:
    {
      // Generate the source file and the debugging information in tandem

      unsigned LineIndex = 1;
      unsigned MetadataKind = Type == DebugInfoType::PTC ?
        PTCInstrMDKind : OriginalInstrMDKind;

      MDString *Last = nullptr;
      std::ofstream Source(DebugPath);
      for (Function &CurrentFunction : TheModule->functions()) {
        if (DISubprogram *CurrentSubprogram = CurrentFunction.getSubprogram()) {
          for (BasicBlock& Block : CurrentFunction) {
            for (Instruction& Instruction : Block) {
              MDString *Body = getMD(&Instruction, MetadataKind);

              if (Body != nullptr && Last != Body) {
                Last = Body;
                std::string BodyString = Body->getString().str();

                Source << BodyString;

                auto *Location = DILocation::get(TheModule->getContext(),
                                                 LineIndex,
                                                 0,
                                                 CurrentSubprogram);
                Instruction.setMetadata(DbgMDKind, Location);
                LineIndex += std::count(BodyString.begin(),
                                        BodyString.end(),
                                        '\n');
              }
            }
          }
        }
      }

      Builder.finalize();
      break;
    }
  case DebugInfoType::LLVMIR:
    {
      // Use the annotator to obtain line and column of the textual LLVM IR for
      // each instruction. Discard the output since it will contain errors,
      // regenerating it later will give a correct result.
      Builder.finalize();

      raw_null_ostream NullStream;
      TheModule->print(NullStream, annotator(true /* DebugInfo */));

      std::ofstream Output(DebugPath);
      raw_os_ostream Stream(Output);
      TheModule->print(Stream, annotator(false));

      break;
    }
  default:
    break;
  }

}

void DebugHelper::print(std::ostream& Output, bool DebugInfo) {
  raw_os_ostream OutputStream(Output);
  TheModule->print(OutputStream, annotator(DebugInfo));
}

bool DebugHelper::copySource() {
  // If debug info refer to LLVM IR, just copy the output file
  if (Type == DebugInfoType::LLVMIR && DebugPath != OutputPath) {
    std::ifstream Source(DebugPath, std::ios::binary);
    std::ofstream Destination(OutputPath, std::ios::binary);

    Destination << Source.rdbuf();

    return true;
  }

  return false;
}

DebugAnnotationWriter *DebugHelper::annotator(bool DebugInfo) {
  Annotator.reset(new DebugAnnotationWriter(TheModule->getContext(),
                                            DebugInfo));
  return Annotator.get();
}
