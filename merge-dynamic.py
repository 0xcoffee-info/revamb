#!/usr/bin/env python

# * DT_GNUHASH?
# * DR_{INIT,FINI}_ARRAY?

import argparse
import os
import shutil
import stat
import struct
import sys

from io import BytesIO

from pprint import pprint

from elftools.elf.elffile import ELFFile
from elftools.elf.constants import P_FLAGS
from elftools.elf.enums import ENUM_P_TYPE

def set_executable(path):
  st = os.stat(path)
  os.chmod(path, st.st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

def rebuild_r_info(relocation, is64):
  if is64:
    relocation.r_info = (relocation.r_info_sym << 32) | relocation.r_info_type
  else:
    relocation.r_info = (relocation.r_info_sym << 8) | relocation.r_info_type

def file_size(file):
  file.seek(0, 2)
  return file.tell()

def only(iterable):
  iterable = list(iterable)
  assert len(iterable) == 1
  return iterable[0]

def read_at_offset(file, start, size):
  file.seek(start)
  return file.read(size)

def chunks(list, size):
  for i in range(0, len(list), size):
    yield list[i:i + size]

def parse(buffer, struct):
  struct_size = struct.sizeof()
  assert len(buffer) % struct_size == 0
  return list(map(struct.parse, chunks(buffer, struct_size)))

def serialize(list, struct):
  return b"".join(map(struct.build, list))

class ParsedElf:
  def __init__(self, file):
    self.file = file
    self.elf = ELFFile(file)

    self.is_dynamic = len(self.segment_by_type("PT_DYNAMIC")) != 0
    if not self.is_dynamic:
      return

    self.segments = list(self.elf.iter_segments())
    self.sections = list(self.elf.iter_sections())

    self.dynamic = only(self.segment_by_type("PT_DYNAMIC"))
    self.is_rela = self.tag("DT_PLTREL") == 7

    self.dynstr = self.read_section("DT_STRTAB", "DT_STRSZ")
    if self.is_rela:
      self.reldyn = self.read_section("DT_RELA", "DT_RELASZ")
      self.relstruct = self.elf.structs.Elf_Rela
    else:
      self.reldyn = self.read_section("DT_REL", "DT_RELSZ")
      self.relstruct = self.elf.structs.Elf_Rel
    self.reldyn_relocations = parse(self.reldyn, self.relstruct)

    self.relplt = self.read_section("DT_JMPREL", "DT_PLTRELSZ")
    self.relplt_relocations = parse(self.relplt, self.relstruct)

    self.symbols_count = max([relocation.r_info_sym
                  for relocation
                  in self.reldyn_relocations + self.relplt_relocations]) + 1

    self.dynsym = self.read_section("DT_SYMTAB",
                                    "DT_SYMENT",
                                    self.symbols_count)
    self.symbols = parse(self.dynsym, self.elf.structs.Elf_Sym)

    self.gnuversion = self.read_section("DT_VERSYM",
                                        scale=self.symbols_count * 2)
    self.gnuversion_indices = self.parse_ints(self.gnuversion, 2)

    if self.has_tag("DT_VERNEED"):
      self.verneeds = []
      verneed_count = self.tag("DT_VERNEEDNUM")
      self.seek_address(self.tag("DT_VERNEED"))
      verneed_position = self.current_position()

      verneed_struct = self.elf.structs.Elf_Verneed
      vernaux_struct = self.elf.structs.Elf_Vernaux

      for _ in range(verneed_count):
        verneed = self.read_struct(verneed_struct)

        vernauxs = []
        vernaux_position = verneed_position + verneed.vn_aux
        self.file.seek(vernaux_position)
        for _ in range(verneed.vn_cnt):
          vernaux = self.read_struct(vernaux_struct)
          vernauxs.append(vernaux)

          vernaux_position += vernaux.vna_next
          self.file.seek(vernaux_position)

        self.verneeds.append((verneed, vernauxs))

        verneed_position += verneed.vn_next
        self.file.seek(verneed_position)

  def serialize_verneeds(self, list):
    stream = BytesIO()
    verneed_struct = self.elf.structs.Elf_Verneed
    vernaux_struct = self.elf.structs.Elf_Vernaux

    verneed_position = 0
    for verneed in list:
      stream.write(verneed_struct.build(verneed[0]))

      vernaux_position = verneed_position + verneed[0].vn_aux
      for vernaux in verneed[1]:
        stream.seek(vernaux_position)
        stream.write(vernaux_struct.build(vernaux))
        vernaux_position += vernaux.vna_next

      verneed_position += verneed[0].vn_next
      stream.seek(verneed_position)

    return stream.getvalue()

  def parse_ints(self, buffer, size):
    assert len(buffer) % size == 0
    size_map = {1: "B", 2: "H", 4: "I", 8: "Q"}
    if self.elf.little_endian:
      format = "<" + size_map[size]
    else:
      format = ">" + size_map[size]
    return [struct.unpack(format, chunk)[0] for chunk in chunks(buffer, size)]

  def serialize_ints(self, list, size):
    size_map = {1: "B", 2: "H", 4: "I", 8: "Q"}
    if self.elf.little_endian:
      format = "<" + size_map[size]
    else:
      format = ">" + size_map[size]
    return b"".join([struct.pack(format, number) for number in list])

  def current_position(self):
    return self.file.tell()

  def read_struct(self, struct):
    buffer = self.file.read(struct.sizeof())
    assert len(buffer) == struct.sizeof()
    return only(parse(buffer, struct))

  def read_section(self, address_tag, size_tag=None, scale=1):
    if size_tag is not None:
      if not self.has_tag(size_tag):
        return bytes();
      else:
        size = self.tag(size_tag) * scale
    else:
      size = scale

    if self.has_tag(address_tag):
      return self.read_address(self.tag(address_tag), size)
    else:
      return bytes()

  def seek_address(self, address):
    self.file.seek(only(self.elf.address_offsets(address)))

  def read_address(self, address, size):
    self.seek_address(address)
    result = self.file.read(size)
    assert len(result) == size
    return result

  def segment_by_type(self, type):
    return [segment
            for segment
            in self.elf.iter_segments()
            if segment.header.p_type == type]

  def dt_by_tag(self, search):
    return [tag
            for tag
            in self.dynamic.iter_tags()
            if tag.entry.d_tag == search]

  def tag(self, tag):
    return only(self.dt_by_tag(tag)).entry.d_val

  def has_tag(self, tag):
    return len(self.dt_by_tag(tag)) != 0

def align(start, alignment):
  return ((start + alignment - 1) / alignment) * alignment

def main():
  parser = argparse.ArgumentParser(description=("Merge the dynamic portions of "
                                                + "the translate ELF with the "
                                                + "one from the host ELF."))
  parser.add_argument("to_extend",
                      metavar="TO_EXTEND",
                      help="The destination ELF.")
  parser.add_argument("source",
                      metavar="SOURCE",
                      help="The source ELF.")
  parser.add_argument("output",
                      metavar="OUTPUT",
                      nargs="?",
                      default="-",
                      help="The output ELF.")
  args = parser.parse_args()

  with (sys.stdout
        if args.output == "-"
        else open(args.output, "wb")) as output_file, \
       open(args.source, "rb") as source_file, \
       open(args.to_extend, "rb") as to_extend_file:
    to_extend_elf = ParsedElf(to_extend_file)
    source_elf = ParsedElf(source_file)

    # If the original ELF was not dynamic, we don't have to do anything
    if not source_elf.is_dynamic:
      to_extend_elf.file.seek(0)
      shutil.copyfileobj(to_extend_elf.file, output_file)
      if args.output != "-":
        set_executable(args.output)
      return 0

    assert to_extend_elf.is_dynamic

    # Prepare new .dynstr
    new_dynstr = to_extend_elf.dynstr
    dynstr_offset = len(new_dynstr)
    assert new_dynstr[-1] == b"\x00"
    new_dynstr += source_elf.dynstr

    to_extend_size = file_size(to_extend_file)
    alignment = 0x1000
    padding = align(to_extend_size, alignment) - to_extend_size
    new_dynstr_offset = to_extend_size + padding

    # Prepare new .dynsym
    new_dynsym = to_extend_elf.dynsym
    dynsym_offset = len(to_extend_elf.symbols)
    new_symbols = list(source_elf.symbols)
    for symbol in new_symbols:
      symbol.st_name += dynstr_offset
    new_dynsym += serialize(new_symbols, source_elf.elf.structs.Elf_Sym)
    new_dynsym_offset =  (new_dynstr_offset
                + len(new_dynstr))

    # Prepare new .dynrel
    new_reldyn = to_extend_elf.reldyn
    new_relocations = (source_elf.relplt_relocations
                       + source_elf.reldyn_relocations)
    for relocation in new_relocations:
      relocation.r_info_sym += dynsym_offset
      rebuild_r_info(relocation, to_extend_elf.elf.elfclass == 64)
    new_reldyn += serialize(new_relocations, source_elf.relstruct)
    new_reldyn_offset = (new_dynsym_offset + len(new_dynsym))

    # 1. Find the highest version index in to_extend_elf
    version_index_offset = 0
    for verneed in to_extend_elf.verneeds:
      for vernaux in verneed[1]:
        version_index_offset = max(version_index_offset, vernaux.vna_other)
    version_index_offset -= 1

    # 2. Go though all the version indexes of source_elf and, unless they
    #  are 0 or 1, increase them by the previous value
    # 3. Concat .gnu.version
    new_gnuversion_offset = new_reldyn_offset + len(new_reldyn)
    new_gnuversion = to_extend_elf.gnuversion
    new_gnuversion_indices = source_elf.gnuversion_indices
    for index, value in enumerate(new_gnuversion_indices):
      if value != 0 and value != 1:
        new_gnuversion_indices[index] += version_index_offset
    new_gnuversion += source_elf.serialize_ints(new_gnuversion_indices, 2)

    # 4. Go through .gnu.version_r and, for each verneed add the string
    #  table offset to the library name.
    # 5. Go through each Vernaux and increment vna_name
    new_verneeds = to_extend_elf.verneeds

    # Find the start position of the last verneed
    position = 0
    for verneed in new_verneeds:
      position += verneed[0].vn_next

    # Update the pointer to the next element of the last verneed to the end
    # of the buffer
    new_verneeds_size = len(to_extend_elf.serialize_verneeds(new_verneeds))
    new_verneeds[-1][0].vn_next = new_verneeds_size - position
    new_gnuversion_r_offset = new_gnuversion_offset + len(new_gnuversion)

    # Fix verneeds and vernaux in source_elf
    for verneed in source_elf.verneeds:
      verneed[0].vn_file += dynstr_offset
      for vernaux in verneed[1]:
        vernaux.vna_name += dynstr_offset
        vernaux.vna_other += version_index_offset
    new_verneeds += source_elf.verneeds
    new_gnuversion_r = source_elf.serialize_verneeds(new_verneeds)

    # Prepare new section headers
    start_address = min([segment.header.p_vaddr
                         for segment
                         in to_extend_elf.segment_by_type("PT_LOAD")])
    start_address += new_dynstr_offset
    assert start_address == align(start_address, alignment)

    # Prepare new .dynamic
    new_dynamic_tags = [dt for dt in to_extend_elf.dynamic.iter_tags()]

    last_dt_needed = -1
    libraries = set()
    to_address = lambda offset: start_address + offset - new_dynstr_offset
    for index, dynamic_tag in enumerate(new_dynamic_tags):
      if dynamic_tag.entry.d_tag == "DT_STRTAB":
        dynamic_tag.entry.d_val = to_address(new_dynstr_offset)
      elif dynamic_tag.entry.d_tag == "DT_STRSZ":
        dynamic_tag.entry.d_val = len(new_dynstr)
      elif dynamic_tag.entry.d_tag in ["DT_REL", "DT_RELA"]:
        dynamic_tag.entry.d_val = to_address(new_reldyn_offset)
      elif dynamic_tag.entry.d_tag in ["DT_RELSZ", "DT_RELASZ"]:
        dynamic_tag.entry.d_val = len(new_reldyn)
      elif dynamic_tag.entry.d_tag == "DT_SYMTAB":
        dynamic_tag.entry.d_val = to_address(new_dynsym_offset)
      elif dynamic_tag.entry.d_tag == "DT_NEEDED":
        last_dt_needed = index
        libraries.add(dynamic_tag.needed)
      elif dynamic_tag.entry.d_tag == "DT_VERNEED":
        dynamic_tag.entry.d_val = to_address(new_gnuversion_r_offset)
      elif dynamic_tag.entry.d_tag == "DT_VERNEEDNUM":
        dynamic_tag.entry.d_val = len(new_verneeds)
      elif dynamic_tag.entry.d_tag == "DT_VERSYM":
        dynamic_tag.entry.d_val = to_address(new_gnuversion_offset)

    # This is done a link-time
    # for dt_needed in reversed(source_elf.dt_by_tag("DT_NEEDED")):
    #   if not dt_needed.needed in libraries:
    #     dt_needed.entry.d_val += dynstr_offset
    #     new_dynamic_tags.insert(last_dt_needed + 1, dt_needed)

    new_dynamic_tags = [dt.entry for dt in new_dynamic_tags]

    new_dynamic = serialize(new_dynamic_tags, source_elf.elf.structs.Elf_Dyn)
    new_dynamic_offset = new_gnuversion_r_offset + len(new_gnuversion_r)

    new_section_headers_offset = new_dynamic_offset + len(new_dynamic)
    new_sections = to_extend_elf.sections
    for section in new_sections:
      if section.name == ".dynstr":
        section.header.sh_addr = to_address((new_dynstr_offset))
        section.header.sh_offset = new_dynstr_offset
        section.header.sh_size = len(new_dynstr)
      elif section.name == ".dynsym":
        section.header.sh_addr = to_address((new_dynsym_offset))
        section.header.sh_offset = new_dynsym_offset
        section.header.sh_size = len(new_dynsym)
      elif section.name in [".rela.dyn", ".rel.dyn"]:
        section.header.sh_addr = to_address((new_reldyn_offset))
        section.header.sh_offset = new_reldyn_offset
        section.header.sh_size = len(new_reldyn)
      elif section.name == ".dynamic":
        section.header.sh_addr = to_address((new_dynamic_offset))
        section.header.sh_offset = new_dynamic_offset
        section.header.sh_size = len(new_dynamic)
      elif section.name == ".gnu.version":
        section.header.sh_addr = to_address((new_gnuversion_offset))
        section.header.sh_offset = new_gnuversion_offset
        section.header.sh_size = len(new_gnuversion)
      elif section.name == ".gnu.version_r":
        section.header.sh_addr = to_address((new_gnuversion_r_offset))
        section.header.sh_offset = new_gnuversion_r_offset
        section.header.sh_size = len(new_gnuversion_r)
        section.header.sh_info = len(new_verneeds)
    new_section_headers = serialize([section.header
                                     for section
                                     in new_sections],
                                    source_elf.elf.structs.Elf_Shdr)

    # Prepare new program headers
    new_program_headers_offset = (new_section_headers_offset
                                  + len(new_section_headers))

    segment_header_size = source_elf.elf.structs.Elf_Phdr.sizeof()
    new_segments = [segment.header for segment in to_extend_elf.segments]
    new_program_headers_size =(len(new_segments) + 1) * segment_header_size

    for segment in new_segments:
      if segment.p_type == "PT_DYNAMIC":
        segment.p_filesz = len(new_dynamic)
        segment.p_memsz = len(new_dynamic)
        segment.p_paddr = to_address((new_dynamic_offset))
        segment.p_vaddr = to_address((new_dynamic_offset))
        segment.p_offset = new_dynamic_offset
      elif segment.p_type == "PT_PHDR":
        segment.p_filesz = new_program_headers_size
        segment.p_memsz = new_program_headers_size
        segment.p_paddr = to_address((new_program_headers_offset))
        segment.p_vaddr = to_address((new_program_headers_offset))
        segment.p_offset = new_program_headers_offset

    new_segment_size = (new_program_headers_offset
                        + new_program_headers_size
                        - new_dynstr_offset)

    zeros = "\x00" * segment_header_size
    new_segment = source_elf.elf.structs.Elf_Phdr.parse(zeros)
    new_segment.p_type = ENUM_P_TYPE["PT_LOAD"]
    new_segment.p_memsz = new_segment_size
    new_segment.p_flags = P_FLAGS.PF_R | P_FLAGS.PF_W
    new_segment.p_offset = new_dynstr_offset
    new_segment.p_vaddr = start_address
    new_segment.p_align = alignment
    new_segment.p_filesz = new_segment_size
    new_segment.p_paddr = start_address
    new_segments += [new_segment]
    new_program_headers = serialize(new_segments,
                                    source_elf.elf.structs.Elf_Phdr)

    # Prepare new ELF header
    new_elf_header = to_extend_elf.elf.header
    new_elf_header.e_phnum = len(new_segments)
    new_elf_header.e_phoff = new_program_headers_offset
    new_elf_header.e_shnum = len(new_sections)
    new_elf_header.e_shoff = new_section_headers_offset
    new_elf_header = to_extend_elf.elf.structs.Elf_Ehdr.build(new_elf_header)

    # Write new ELF header
    output_file.write(new_elf_header)

    # Write rest of the to_extend file
    to_extend_elf.file.seek(len(new_elf_header))
    shutil.copyfileobj(to_extend_elf.file, output_file)

    # Align to page
    output_file.write(b"\x00" * padding)

    # Write new .dynstr
    assert output_file.tell() == new_dynstr_offset
    output_file.write(new_dynstr)

    # Write new .dynsym
    assert output_file.tell() == new_dynsym_offset
    output_file.write(new_dynsym)

    # Write new .rel.dyn
    assert output_file.tell() == new_reldyn_offset
    output_file.write(new_reldyn)

    # Write new .rel.dyn
    assert output_file.tell() == new_gnuversion_offset
    output_file.write(new_gnuversion)

    # Write new .rel.dyn
    assert output_file.tell() == new_gnuversion_r_offset
    output_file.write(new_gnuversion_r)

    # Write new .dynamic
    assert output_file.tell() == new_dynamic_offset
    output_file.write(new_dynamic)

    # Write new section headers
    assert output_file.tell() == new_section_headers_offset
    output_file.write(new_section_headers)

    # Write new program headers
    assert output_file.tell() == new_program_headers_offset
    output_file.write(new_program_headers)

  if args.output != "-":
    set_executable(args.output)

  return 0

if __name__ == "__main__":
  sys.exit(main())
