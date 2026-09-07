using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
using Mono.Cecil;
using Mono.Cecil.Cil;

public static class HighrookStatDisplayPatch {
    private const uint ShowCardInfoToken = 0x0600008c;
    private const uint GetDisplayNameToken = 0x06000159;
    private const int FieldOccurrence = 226;

    private static IEnumerable<TypeDefinition> Types(IEnumerable<TypeDefinition> roots) {
        foreach (var type in roots) {
            yield return type;
            foreach (var nested in Types(type.NestedTypes)) yield return nested;
        }
    }

    private static string Decode(string value) {
        return Encoding.UTF8.GetString(Convert.FromBase64String(value));
    }

    private static List<KeyValuePair<string, string>> ReadMap(string path) {
        var result = new List<KeyValuePair<string, string>>();
        foreach (var line in File.ReadAllLines(path, Encoding.ASCII)) {
            if (String.IsNullOrWhiteSpace(line)) continue;
            var fields = line.Split('\t');
            if (fields.Length != 2) throw new InvalidDataException("invalid stat map TSV");
            result.Add(new KeyValuePair<string, string>(Decode(fields[0]), Decode(fields[1])));
        }
        var expected = new[] { "Injury", "Fatigue", "Madness", "DiseaseA" };
        if (!result.Select(x => x.Key).SequenceEqual(expected) || result.Any(x => String.IsNullOrWhiteSpace(x.Value)))
            throw new InvalidDataException("stat map must contain exact ordered raw identifiers");
        return result;
    }

    private static string OperandKey(object operand, Dictionary<Instruction, int> positions) {
        if (operand == null) return "null";
        var text = operand as string;
        if (text != null) return "s:" + text;
        var target = operand as Instruction;
        if (target != null) return "i:" + positions[target].ToString(CultureInfo.InvariantCulture);
        var targets = operand as Instruction[];
        if (targets != null) return "is:" + String.Join(",", targets.Select(x => positions[x].ToString(CultureInfo.InvariantCulture)).ToArray());
        var variable = operand as VariableDefinition;
        if (variable != null) return "v:" + variable.Index;
        var parameter = operand as ParameterDefinition;
        if (parameter != null) return "p:" + parameter.Index;
        var member = operand as MemberReference;
        if (member != null) return "m:" + member.FullName;
        return "o:" + Convert.ToString(operand, CultureInfo.InvariantCulture);
    }

    private static string[] Fingerprint(MethodDefinition method, int skipStart, int skipCount) {
        var kept = method.Body.Instructions.Where((x, n) => n < skipStart || n >= skipStart + skipCount).ToList();
        var positions = kept.Select((x, n) => new { x, n }).ToDictionary(x => x.x, x => x.n);
        return kept.Select(x => x.OpCode.Code + "|" + OperandKey(x.Operand, positions)).ToArray();
    }

    private static string MethodKey(MethodDefinition method) {
        return method.DeclaringType.FullName + "|" + method.FullName;
    }

    private static Dictionary<string, string[]> Snapshot(ModuleDefinition module) {
        return Types(module.Types).SelectMany(x => x.Methods).Where(x => x.HasBody)
            .ToDictionary(MethodKey, x => Fingerprint(x, Int32.MaxValue, 0));
    }

    private static MethodDefinition StableMethod(ModuleDefinition module, string typeName, string methodName) {
        return Types(module.Types).Where(x => x.Name == typeName).SelectMany(x => x.Methods)
            .SingleOrDefault(x => x.Name == methodName);
    }

    private static void VerifyFieldSite(MethodDefinition target) {
        if (target == null || !target.HasBody || target.Name != "ShowCardInfo" || target.DeclaringType.Name != "C_CardInspector")
            throw new InvalidDataException("ShowCardInfo method identity mismatch");
        var instructions = target.Body.Instructions;
        if (instructions.Count <= FieldOccurrence + 1 || instructions[FieldOccurrence].OpCode.Code != Code.Ldfld ||
            ((FieldReference)instructions[FieldOccurrence].Operand).FullName != "System.String C_SO_Card::m_HourTickStatMod" ||
            instructions[FieldOccurrence - 2].OpCode.Code != Code.Ldarg_1 || instructions[FieldOccurrence - 1].OpCode.Code != Code.Ldfld ||
            ((FieldReference)instructions[FieldOccurrence - 1].Operand).FullName != "C_SO_Card C_CardBase::m_CardDef" ||
            instructions[FieldOccurrence + 1].OpCode.Code != Code.Stelem_Ref)
            throw new InvalidDataException("ShowCardInfo hour-tick display field site mismatch");
    }

    public static string Apply(string inputPath, string outputPath, string pristinePath, string mapPath) {
        var map = ReadMap(mapPath);
        Dictionary<string, string[]> before;
        string targetKey;
        int insertedCount = map.Count * 8 + 1;
        using (var pristine = AssemblyDefinition.ReadAssembly(pristinePath)) {
            var sourceTarget = Types(pristine.MainModule.Types).SelectMany(x => x.Methods).SingleOrDefault(x => x.MetadataToken.ToUInt32() == ShowCardInfoToken);
            VerifyFieldSite(sourceTarget);
            var sourceDisplay = Types(pristine.MainModule.Types).SelectMany(x => x.Methods).SingleOrDefault(x => x.MetadataToken.ToUInt32() == GetDisplayNameToken);
            if (sourceDisplay == null || sourceDisplay.Name != "GetDisplayNameForTag" || sourceDisplay.DeclaringType.Name != "C_Utilities")
                throw new InvalidDataException("pristine display helper token mismatch");
        }
        using (var assembly = AssemblyDefinition.ReadAssembly(inputPath)) {
            var module = assembly.MainModule;
            before = Snapshot(module);
            var target = StableMethod(module, "C_CardInspector", "ShowCardInfo");
            VerifyFieldSite(target);
            targetKey = MethodKey(target);
            var instructions = target.Body.Instructions;
            var display = StableMethod(module, "C_Utilities", "GetDisplayNameForTag");
            if (display == null || display.Name != "GetDisplayNameForTag") throw new InvalidDataException("display helper identity mismatch");
            var equality = display.Body.Instructions.Where(x => x.OpCode.Code == Code.Call)
                .Select(x => x.Operand as MethodReference).SingleOrDefault(x => x != null && x.FullName == "System.Boolean System.String::op_Equality(System.String,System.String)");
            if (equality == null) throw new InvalidDataException("string equality reference not found");
            var processor = target.Body.GetILProcessor();
            var cursor = instructions[FieldOccurrence];
            var end = Instruction.Create(OpCodes.Nop);
            foreach (var pair in map) {
                var next = Instruction.Create(OpCodes.Nop);
                var additions = new[] {
                    Instruction.Create(OpCodes.Dup), Instruction.Create(OpCodes.Ldstr, pair.Key),
                    Instruction.Create(OpCodes.Call, equality), Instruction.Create(OpCodes.Brfalse, next),
                    Instruction.Create(OpCodes.Pop), Instruction.Create(OpCodes.Ldstr, pair.Value),
                    Instruction.Create(OpCodes.Br, end), next
                };
                foreach (var addition in additions) { processor.InsertAfter(cursor, addition); cursor = addition; }
            }
            processor.InsertAfter(cursor, end);
            assembly.Write(outputPath);
        }
        using (var reloaded = AssemblyDefinition.ReadAssembly(outputPath)) {
            var after = Snapshot(reloaded.MainModule);
            if (!before.Keys.OrderBy(x => x).SequenceEqual(after.Keys.OrderBy(x => x)))
                throw new InvalidDataException("method identity set changed");
            var target = Types(reloaded.MainModule.Types).SelectMany(x => x.Methods).Single(x => MethodKey(x) == targetKey);
            foreach (var key in before.Keys) {
                var actual = key == targetKey ? Fingerprint(target, FieldOccurrence + 1, insertedCount) : after[key];
                if (!before[key].SequenceEqual(actual)) {
                    var limit = Math.Min(before[key].Length, actual.Length);
                    var mismatch = Enumerable.Range(0, limit).FirstOrDefault(x => before[key][x] != actual[x]);
                    throw new InvalidDataException("non-display IL changed: " + key + ":" + mismatch + " before=" + before[key][mismatch] + " after=" + actual[mismatch]);
                }
            }
            if (target.Body.Instructions.Count != before[targetKey].Length + insertedCount)
                throw new InvalidDataException("unexpected inserted instruction count");
        }
        return "display_site=0x0600008c:226;inserted=" + insertedCount + ";mechanics_unchanged=true;unknown_preserved=true";
    }
}
