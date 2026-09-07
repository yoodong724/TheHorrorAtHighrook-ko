using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using Mono.Cecil;
using Mono.Cecil.Cil;

public static class HighrookExtraIlPatch {
    private sealed class Patch {
        public string Id, Type, Method, Source, SourceHash, Replacement, StableMethodKey;
        public uint Token;
        public int Occurrence, Offset;
    }

    private static string Decode(string value) {
        return Encoding.UTF8.GetString(Convert.FromBase64String(value));
    }

    private static string Hash(string value) {
        using (var sha = SHA256.Create()) {
            return BitConverter.ToString(sha.ComputeHash(Encoding.UTF8.GetBytes(value))).Replace("-", "").ToLowerInvariant();
        }
    }

    private static IEnumerable<TypeDefinition> Types(IEnumerable<TypeDefinition> roots) {
        foreach (var type in roots) {
            yield return type;
            foreach (var nested in Types(type.NestedTypes)) yield return nested;
        }
    }

    private static string OperandKey(object operand) {
        if (operand == null) return "null";
        var text = operand as string;
        if (text != null) return "s:" + text;
        var instruction = operand as Instruction;
        if (instruction != null) return "i:" + instruction.Offset;
        var instructions = operand as Instruction[];
        if (instructions != null) return "is:" + String.Join(",", instructions.Select(x => x.Offset.ToString()).ToArray());
        var variable = operand as VariableDefinition;
        if (variable != null) return "v:" + variable.Index;
        var parameter = operand as ParameterDefinition;
        if (parameter != null) return "p:" + parameter.Index;
        var member = operand as MemberReference;
        if (member != null) return "m:" + member.GetType().Name + ":" + member.FullName;
        var callSite = operand as CallSite;
        if (callSite != null) return "c:" + callSite.FullName;
        var provider = operand as IMetadataTokenProvider;
        if (provider != null) return "t:" + provider.GetType().Name + ":" + provider.MetadataToken.ToUInt32().ToString("x8");
        return "o:" + Convert.ToString(operand, CultureInfo.InvariantCulture);
    }

    private static string MethodKey(MethodDefinition method) {
        return method.DeclaringType.FullName + "|" + method.FullName;
    }

    private static Dictionary<string, string[]> Snapshot(ModuleDefinition module) {
        var result = new Dictionary<string, string[]>();
        foreach (var method in Types(module.Types).SelectMany(x => x.Methods).Where(x => x.HasBody)) {
            result[MethodKey(method)] = method.Body.Instructions
                .Select(x => x.OpCode.Code + "|" + OperandKey(x.Operand)).ToArray();
        }
        return result;
    }

    private static List<Patch> ReadPatches(string tsvPath) {
        var result = new List<Patch>();
        foreach (var line in File.ReadAllLines(tsvPath, Encoding.UTF8)) {
            if (String.IsNullOrWhiteSpace(line)) continue;
            var p = line.Split('\t');
            if (p.Length != 9) throw new InvalidDataException("invalid patch TSV");
            result.Add(new Patch {
                Id = Decode(p[0]), Type = Decode(p[1]), Method = Decode(p[2]),
                Token = UInt32.Parse(Decode(p[3]).Substring(2), NumberStyles.HexNumber),
                Occurrence = Int32.Parse(Decode(p[4])), Offset = Int32.Parse(Decode(p[5])),
                Source = Decode(p[6]), SourceHash = Decode(p[7]), Replacement = Decode(p[8])
            });
        }
        return result;
    }

    public static string Apply(string inputPath, string outputPath, string tsvPath) {
        var patches = ReadPatches(tsvPath);
        if (patches.Select(x => x.Id).Distinct().Count() != patches.Count) throw new InvalidDataException("duplicate patch ID");
        Dictionary<string, string[]> before;
        using (var assembly = AssemblyDefinition.ReadAssembly(inputPath)) {
            before = Snapshot(assembly.MainModule);
            foreach (var patch in patches) {
                var method = Types(assembly.MainModule.Types).SelectMany(x => x.Methods)
                    .SingleOrDefault(x => x.MetadataToken.ToUInt32() == patch.Token);
                if (method == null || !method.HasBody || method.Name != patch.Method || method.DeclaringType.Name != patch.Type)
                    throw new InvalidDataException("method locator mismatch: " + patch.Id);
                patch.StableMethodKey = MethodKey(method);
                if (patch.Occurrence < 0 || patch.Occurrence >= method.Body.Instructions.Count)
                    throw new InvalidDataException("instruction occurrence outside method: " + patch.Id);
                var instruction = method.Body.Instructions[patch.Occurrence];
                if (instruction.Offset != patch.Offset || instruction.OpCode.Code != Code.Ldstr || (string)instruction.Operand != patch.Source)
                    throw new InvalidDataException("ldstr locator/source mismatch: " + patch.Id);
                if (Hash(patch.Source) != patch.SourceHash) throw new InvalidDataException("source hash mismatch: " + patch.Id);
                instruction.Operand = patch.Replacement;
            }
            assembly.Write(outputPath);
        }
        using (var reloaded = AssemblyDefinition.ReadAssembly(outputPath)) {
            var after = Snapshot(reloaded.MainModule);
            if (!before.Keys.OrderBy(x => x).SequenceEqual(after.Keys.OrderBy(x => x)))
                throw new InvalidDataException("method token set changed");
            var bySite = patches.ToDictionary(x => x.StableMethodKey + "\n" + x.Occurrence, x => x);
            foreach (var methodKey in before.Keys) {
                if (before[methodKey].Length != after[methodKey].Length) throw new InvalidDataException("instruction count changed at " + methodKey + " before=" + before[methodKey].Length + " after=" + after[methodKey].Length);
                for (var i = 0; i < before[methodKey].Length; i++) {
                    Patch patch;
                    if (bySite.TryGetValue(methodKey + "\n" + i, out patch)) {
                        if (after[methodKey][i] != "Ldstr|s:" + patch.Replacement) throw new InvalidDataException("patched value reload mismatch: " + patch.Id);
                    } else if (before[methodKey][i] != after[methodKey][i]) {
                        throw new InvalidDataException("non-target IL changed at " + methodKey + ":" + i + " before=" + before[methodKey][i] + " after=" + after[methodKey][i]);
                    }
                }
            }
        }
        return "patched=" + patches.Count + ";semantic_non_targets_preserved=true";
    }
}
