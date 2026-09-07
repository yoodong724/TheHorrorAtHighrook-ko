#!/usr/bin/env python3
"""Stage 8 full-corpus Highrook builder; all game outputs remain in memory."""
from __future__ import annotations
import hashlib, importlib.util, io, json, os, re, shutil, struct, sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

ROOT=Path(__file__).resolve().parents[3]
CORPUS=ROOT/'localization/corpus'
REVISION='sha256:c60ced6544a45a98745ca6ebcede78326382713f1a0781e8eca8bcd6dc27efd4'
DATA='TheHorrorAtHighrook_Data/'
ASSEMBLY=DATA+'Managed/Assembly-CSharp.dll'
TMP_DLL=DATA+'Managed/Unity.TextMeshPro.dll'
TMP_DLL_SHA='b027292d5ca1dfeaa3e5c2cf928d0924e4b09e6bd47f63e374938f435534e6b1'
CORPUS_HASHES={'source-manifest.json': '2f9dfbb7f3613d9e4a66e741ba11df93315fd0763f699005e0f9b751c32b3584', 'locator-sidecar.json': '7565ca0231b9cb6d15a3507e29b46d2ededca9d1b11fad47bec55c67b86dcb0b', 'segments.jsonl': 'f7a5f021b631f40bf668deb8d785bf7d57ad56830547a2174dc6e8d4c7d9c4e6'}
ADAPTER_HASHES={'work/stage3/adapter/game_patch.py': 'e0846f0e0ea7b93d078d676fb49c7ba4988228866fa54c4d36099666b4a31edb', 'work/stage3/extra_adapter/adapter.py': '5ca6262678d7af06f858683d62357c59d6b546e2d8e3ce436fd4b719b7c9f0a0', 'work/stage3/extra_adapter/patch_il.cs': 'bfdf7649c4e6ef470150d5c153c6e856db43e376323fd5a4b646bc2e56860932', 'work/stage3/extra_adapter/patch_il.ps1': '23a4125785973d2f8adae6ead558e043e800c7fe82546dca505082ac7fa8bf15', 'work/stage2/font/font_patch.py': '89ce57526617f8d3499be6e6dc2f3d58ad405f818a2896c9cf373f26417d86df', 'work/stage8/build/stat_display.py': '1f8a26136e58559caa455896f0f062181ea5cd96334968dac52385310824e2cf', 'work/stage8/build/stat_display.cs': 'c02ba926a96709f153a85db8a1442613ef8b8ae1f1eacbbbe4d6e90f93c2d77b', 'work/stage8/build/stat_display.ps1': '3c8d5a1e437bb38a7bcdabd4991d7680ff8f20363cd04d13328c5bed1a74a3dd'}
SCRATCH_ROOT=ROOT/'work/stage8/build/test-output'

class BuildError(ValueError): pass
def sha(b:bytes)->str:return hashlib.sha256(b).hexdigest()
def canonical(x:Any)->bytes:return (json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':'))+'\n').encode()
def _module(name:str,path:Path)->Any:
 s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);sys.modules[name]=m;s.loader.exec_module(m);return m

@dataclass(frozen=True)
class FullCorpus:
 rows:tuple[dict[str,Any],...]; by_id:dict[str,dict[str,Any]]; sidecar:dict[str,dict[str,Any]]; manifest:dict[str,Any]

def load_full_corpus()->FullCorpus:
 for name,digest in CORPUS_HASHES.items():
  if sha((CORPUS/name).read_bytes())!=digest:raise BuildError('final corpus freeze hash mismatch: '+name)
 for rel,digest in ADAPTER_HASHES.items():
  if sha((ROOT/rel).read_bytes())!=digest:raise BuildError('reviewed adapter hash mismatch: '+rel)
 rows=tuple(json.loads(x) for x in (CORPUS/'segments.jsonl').read_text().splitlines() if x)
 side=json.loads((CORPUS/'locator-sidecar.json').read_text());man=json.loads((CORPUS/'source-manifest.json').read_text())
 by={}
 for r in rows:
  i=r['id']
  if i in by or r['source_revision']!=REVISION or sha(r['source']['text'].encode())!=r['source']['text_sha256']:raise BuildError('invalid/duplicate frozen segment: '+i)
  by[i]=r
 if len(rows)!=2693 or set(by)!=set(side['entries']) or side['source_revision']!=REVISION or man['source_revision']!=REVISION or man.get('final_segment_count')!=2693:raise BuildError('final corpus identity/count mismatch')
 kinds=Counter(e['container_kind'] for e in side['entries'].values())
 if kinds!=Counter({'loose_yaml':2198,'unity_textasset':280,'unity_mono_behaviour_string':192,'managed_il_ldstr':17,'image_asset':6}):raise BuildError('final corpus kind counts changed')
 controls=[r for r in rows if ':CustomString:' in r['id']]
 if len(controls)!=41 or Counter(r['source']['text'] for r in controls)!=Counter({'Full':18,'Cross':15,'Circle':8}):raise BuildError('CustomString control-key scope changed')
 return FullCorpus(rows,by,side['entries'],man)

def source_values(c:FullCorpus)->dict[str,str]:return {i:r['source']['text'] for i,r in c.by_id.items()}

TRANSLATION_FIELDS={'schema_version','id','source_revision','source_sha256','context_pack_id','context_pack_digest','translation_revision','ko','status','translator_role','notes'}
REVIEW_FIELDS={'schema_version','review_id','id','stage','source_revision','source_sha256','context_pack_id','context_pack_digest','reviewed_translation_revision','reviewed_translation_sha256','reviewer_role','reviewer_model','reviewer_effort','verdict','reviewed_areas','issue_ids','suggested_ko','notes'}
IDENTITY_RE=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]*$')

def _strict_object(pairs:list[tuple[str,Any]])->dict[str,Any]:
 out={}
 for key,value in pairs:
  if key in out:raise BuildError('duplicate JSON key in review bytes: '+key)
  out[key]=value
 return out

def _review_rows(data:bytes,label:str)->list[dict[str,Any]]:
 try:text=data.decode('utf-8')
 except UnicodeDecodeError as exc:raise BuildError('review bytes are not UTF-8: '+label) from exc
 try:
  if label.endswith('.jsonl') or (text.lstrip() and text.lstrip()[0] not in '[{'):
   if any(not line.strip() for line in text.splitlines()):raise BuildError('blank JSONL review row: '+label)
   rows=[json.loads(line,object_pairs_hook=_strict_object) for line in text.splitlines()]
  else:
   value=json.loads(text,object_pairs_hook=_strict_object);rows=value if isinstance(value,list) else [value]
 except (json.JSONDecodeError,TypeError) as exc:raise BuildError('invalid review JSON: '+label) from exc
 if not rows or any(not isinstance(row,dict) for row in rows):raise BuildError('empty or non-object review content: '+label)
 return rows

def _validate_translation_shape(rec:dict[str,Any])->None:
 if set(rec)!=TRANSLATION_FIELDS or rec.get('schema_version')!='1.0.0' or rec.get('status')!='release_approved' or not isinstance(rec.get('ko'),str) or not isinstance(rec.get('notes'),list):raise BuildError('release translation record is not release-approved schema shape: '+str(rec.get('id')))
 for key in ('id','source_revision','context_pack_id','translation_revision','translator_role'):
  if not isinstance(rec.get(key),str) or not IDENTITY_RE.fullmatch(rec[key]):raise BuildError('invalid translation field '+key+': '+str(rec.get('id')))
 for key in ('source_sha256','context_pack_digest'):
  if not isinstance(rec.get(key),str) or not re.fullmatch(r'[0-9a-f]{64}',rec[key]):raise BuildError('invalid translation hash field '+key+': '+rec['id'])
 if any(not isinstance(note,str) for note in rec['notes']):raise BuildError('invalid translation notes: '+rec['id'])

def _validate_review_shape(row:dict[str,Any])->None:
 if set(row)!=REVIEW_FIELDS or row.get('schema_version')!='1.0.0' or row.get('stage') not in ('l1','l2') or row.get('verdict') not in ('pending','accept','changes_required','blocked'):raise BuildError('review record is not schema-shaped: '+str(row.get('review_id')))
 for key in ('review_id','id','source_revision','context_pack_id','reviewed_translation_revision','reviewer_role'):
  if not isinstance(row.get(key),str) or not IDENTITY_RE.fullmatch(row[key]):raise BuildError('invalid review field '+key)
 for key in ('source_sha256','context_pack_digest','reviewed_translation_sha256'):
  if not isinstance(row.get(key),str) or not re.fullmatch(r'[0-9a-f]{64}',row[key]):raise BuildError('invalid review hash field '+key)
 if not isinstance(row.get('reviewed_areas'),list) or any(x not in {'meaning','naturalness','terminology','voice','timeline','technical'} for x in row['reviewed_areas']) or not isinstance(row.get('issue_ids'),list) or any(not isinstance(x,str) for x in row['issue_ids']) or not isinstance(row.get('notes'),list) or any(not isinstance(x,str) for x in row['notes']) or row.get('suggested_ko') is not None and not isinstance(row['suggested_ko'],str) or row.get('reviewer_model') is not None and not isinstance(row['reviewer_model'],str) or row.get('reviewer_effort') is not None and not isinstance(row['reviewer_effort'],str):raise BuildError('invalid review list/nullable fields: '+row['review_id'])
 expected_roles={'l1':{'reviewer_l1','reviewer_l1_astra'},'l2':{'reviewer_l2'}}
 if row['reviewer_role'] not in expected_roles[row['stage']]:raise BuildError('reviewer role does not match stage: '+row['review_id'])

def _validate_release_reviews(c:FullCorpus,records:list[dict[str,Any]],reviews:Mapping[str,bytes],provenance:dict[str,Any])->None:
 expected_hashes=provenance.get('review_sha256')
 if not isinstance(expected_hashes,dict) or set(expected_hashes)!=set(reviews) or any(not isinstance(data,bytes) or sha(data)!=expected_hashes[label] for label,data in reviews.items()):raise BuildError('exact review blob set/hash mismatch')
 review_rows=[]
 for label,data in sorted(reviews.items()):review_rows.extend(_review_rows(data,label))
 seen_review_ids=set();by_site={}
 for row in review_rows:
  _validate_review_shape(row)
  if row['review_id'] in seen_review_ids:raise BuildError('duplicate review_id: '+row['review_id'])
  seen_review_ids.add(row['review_id']);site=(row['id'],row['stage'])
  if site in by_site:raise BuildError('duplicate review for ID/stage: '+row['id']+'/'+row['stage'])
  by_site[site]=row
 candidates={r['id']:r for r in records};expected={(i,stage) for i in candidates for stage in ('l1','l2')}
 if set(by_site)!=expected:raise BuildError(f'review coverage mismatch: missing={len(expected-set(by_site))}, unknown={len(set(by_site)-expected)}')
 for (i,stage),row in by_site.items():
  rec=candidates[i];source=c.by_id[i]
  if row['verdict']!='accept':raise BuildError('review verdict is not accept: '+i+'/'+stage)
  if not {'meaning','naturalness'}.issubset(row['reviewed_areas']):raise BuildError('review lacks meaning/naturalness coverage: '+i+'/'+stage)
  if row['source_revision']!=rec['source_revision'] or row['source_sha256']!=source['source']['text_sha256'] or row['context_pack_id']!=rec['context_pack_id'] or row['context_pack_digest']!=rec['context_pack_digest'] or row['reviewed_translation_revision']!=rec['translation_revision'] or row['reviewed_translation_sha256']!=sha(rec['ko'].encode()):raise BuildError('stale or mismatched review: '+i+'/'+stage)

def normalize_translations(c:FullCorpus,translations:Mapping[str,str]|Iterable[Mapping[str,Any]],mode:str,provenance:dict[str,Any]|None=None,reviews:Mapping[str,bytes]|None=None)->dict[str,str]:
 records=[]
 if isinstance(translations,Mapping): items=list(translations.items())
 else:
  records=list(translations);items=[(x['id'],x['ko']) for x in records]
 out={}
 for i,ko in items:
  if i in out:raise BuildError('duplicate translation ID: '+i)
  if not isinstance(i,str) or not isinstance(ko,str):raise BuildError('translation ID/ko must be strings')
  out[i]=ko
 if set(out)!=set(c.by_id):raise BuildError(f'translation coverage mismatch: missing={len(set(c.by_id)-set(out))}, unknown={len(set(out)-set(c.by_id))}')
 for i in out:
  if ':CustomString:' in i and out[i]!=c.by_id[i]['source']['text']:raise BuildError('CustomString control key must remain exact source: '+i)
 for i,ko in out.items():
  r=c.by_id[i];patterns=r['constraints']['token_patterns'];found=re.findall('|'.join('(?:'+p+')' for p in patterns),ko) if patterns else []
  if found!=r['constraints']['protected_tokens']:raise BuildError('protected token sequence changed: '+i)
  if not ko and not r['constraints']['allow_empty']:raise BuildError('empty translation: '+i)
 if mode=='structural':
  if any(out[i]!=c.by_id[i]['source']['text'] for i in out):raise BuildError('structural mode permits source no-change values only')
 elif mode=='release':
  if not records or provenance is None or reviews is None:raise BuildError('release mode requires record provenance and exact L1/L2 review bytes')
  required=('translation_sha256','scope_ids_sha256','review_sha256','approved')
  if any(k not in provenance for k in required) or provenance['approved'] is not True:raise BuildError('release provenance incomplete or not explicitly approved')
  for rec in records:_validate_translation_shape(rec)
  artifact=canonical(sorted(records,key=lambda x:x['id']))
  if sha(artifact)!=provenance['translation_sha256'] or sha(canonical(sorted(out)))!=provenance['scope_ids_sha256']:raise BuildError('translation artifact/scope provenance mismatch')
  for rec in records:
   r=c.by_id[rec['id']]
   if rec['source_revision']!=REVISION or rec['source_sha256']!=r['source']['text_sha256']:raise BuildError('record source provenance mismatch: '+rec['id'])
  _validate_release_reviews(c,records,reviews,provenance)
 elif mode!='test':raise BuildError('mode must be structural, test, or release')
 return out

def normalize_baseline_manifest(doc:Any)->dict[str,str]:
 rows=doc.get('files') if isinstance(doc,dict) and 'files' in doc else doc
 if isinstance(rows,dict): result=dict(rows)
 elif isinstance(rows,list):result={x['path']:x['sha256'] for x in rows}
 else:raise BuildError('invalid baseline manifest')
 if len(result)!=len(rows):raise BuildError('duplicate baseline manifest path')
 for p,h in result.items():
  q=PurePosixPath(p)
  if q.is_absolute() or '..' in q.parts or '\\' in p or not re.fullmatch(r'[0-9a-f]{64}',h):raise BuildError('unsafe baseline manifest entry: '+p)
 return result

def verify_baseline(root:Path,doc:Any,expected_count:int=1684)->dict[str,bytes]:
 root=Path(root);resolved=root.resolve()
 if root.is_symlink() or not root.is_dir():raise BuildError('baseline must be a non-symlink directory')
 expected=normalize_baseline_manifest(doc)
 actual={p.relative_to(root).as_posix():p for p in root.rglob('*') if p.is_file() and not p.is_symlink()}
 if any(p.is_symlink() for p in root.rglob('*')):raise BuildError('baseline symlink forbidden')
 if len(expected)!=expected_count or set(actual)!=set(expected):raise BuildError(f'full baseline inventory mismatch: expected={len(expected)}, actual={len(actual)}')
 data={}
 for rel,p in actual.items():
  b=p.read_bytes()
  if sha(b)!=expected[rel]:raise BuildError('baseline hash mismatch: '+rel)
  data[rel]=b
 if sha(data[TMP_DLL])!=TMP_DLL_SHA:raise BuildError('Unity.TextMeshPro.dll is not the pristine non-diagnostic DLL')
 return data

def _core_projection(full:FullCorpus,core:Any)->Any:
 ids=[i for i,e in full.sidecar.items() if e['container_kind'] in ('loose_yaml','unity_textasset') and ':CustomString:' not in i]
 rows=tuple(full.by_id[i] for i in ids); side={i:full.sidecar[i] for i in ids}; inputs={x['path']:x['sha256'] for x in full.manifest['inputs']}
 return core.Corpus(REVISION,rows,{r['id']:r for r in rows},side,inputs,full.manifest['resources_en']['script_sha256'])

def _extra_facts(full:FullCorpus)->dict[str,dict[str,Any]]:
 out={}
 for i,e in full.sidecar.items():
  if e['container_kind'] in ('unity_mono_behaviour_string','managed_il_ldstr'):
   out[i]={'id':i,'source':full.by_id[i]['source']['text'],'source_sha256':full.by_id[i]['source']['text_sha256'],'classification':e['classification'],'locator':e['locator']}
 return out

def _image_rgba(data:bytes,width:int,height:int)->Any:
 from PIL import Image
 im=Image.open(io.BytesIO(data))
 if im.format!='PNG' or im.size!=(width,height):raise BuildError('replacement must be exact-dimension PNG')
 return im.convert('RGBA')

def _validate_image(spec:dict[str,Any],source_image:Any,width:int,height:int)->Any:
 from PIL import Image,ImageChops,ImageDraw
 data=spec.get('png');
 if not isinstance(data,bytes) or sha(data)!=spec.get('sha256') or (spec.get('width'),spec.get('height'))!=(width,height):raise BuildError('replacement image caller hash/dimensions mismatch')
 out=_image_rgba(data,width,height);regions=spec.get('allowed_regions')
 if not isinstance(regions,list) or not regions:raise BuildError('replacement image requires explicit allowed_regions')
 mask=Image.new('L',(width,height));draw=ImageDraw.Draw(mask)
 for box in regions:
  if len(box)!=4:raise BuildError('invalid allowed image region')
  x,y,w,h=box
  if min(x,y,w,h)<0 or not w or not h or x+w>width or y+h>height:raise BuildError('allowed image region outside source')
  draw.rectangle((x,y,x+w-1,y+h-1),fill=255)
 diff=ImageChops.difference(source_image.convert('RGBA'),out);outside=ImageChops.invert(mask)
 if any(ImageChops.multiply(band,outside).getbbox() for band in diff.split()):raise BuildError('replacement changes pixels outside allowed regions')
 if not any(band.getbbox() for band in diff.split()):raise BuildError('replacement image has no decoded pixel change')
 return out

def _compose_unity(source:bytes,source_path:Path,facts:list[dict[str,Any]],values:dict[str,str],images:list[tuple[str,dict[str,Any],dict[str,Any]]],extra:Any)->tuple[bytes,dict[str,Any]]:
 hashes={f['locator']['container_sha256'] for f in facts}|{e['evidence']['asset_sha256'] for _,e,_ in images}
 if len(hashes)!=1 or sha(source)!=next(iter(hashes)):raise BuildError('Unity composition source hash mismatch')
 UnityPy=extra._load_unitypy();env=UnityPy.load(str(source_path));objects={o.path_id:o for o in env.objects};before={i:o.get_raw_data() for i,o in objects.items()};expected=set()
 groups=defaultdict(list)
 for f in facts:groups[f['locator']['path_id']].append(f)
 for pid,fs in groups.items():
  raw=before[pid]
  if sha(raw)!={f['locator']['object_sha256'] for f in fs}.pop():raise BuildError('extra object hash mismatch')
  for f in fs:
   actual,end=extra._read_string(raw,f['locator']['field_offset'])
   if actual!=f['source'] or end-f['locator']['field_offset']!=f['locator']['field_encoded_size']:raise BuildError('extra field mismatch: '+f['id'])
  for f in sorted(fs,key=lambda x:x['locator']['field_offset'],reverse=True):
   loc=f['locator'];replacement=extra._encoded_string(values[f['id']]);raw=raw[:loc['field_offset']]+replacement+raw[loc['field_offset']+loc['field_encoded_size']:]
  if raw!=before[pid]:objects[pid].set_raw_data(raw);expected.add(pid)
 for resource,e,spec in images:
  ev=e['evidence'];pid=ev['texture_path_id'];tex=objects[pid].read();width,height=tex.m_Width,tex.m_Height
  if tex.m_Name!=ev['texture_name'] or ('width' in ev and (width,height)!=(ev['width'],ev['height'])):raise BuildError('texture identity mismatch: '+resource)
  replacement=_validate_image(spec,tex.image.convert('RGBA'),width,height)
  tex.set_image(replacement,target_format=tex.m_TextureFormat,mipmap_count=getattr(tex,'m_MipCount',1));tex.save();expected.add(pid)
 output=env.file.save();check=UnityPy.load(output);after={o.path_id:o.get_raw_data() for o in check.objects}
 changed={i for i in before if before[i]!=after[i]}
 if changed!=expected:raise BuildError(f'Unity non-target object changed: expected={sorted(expected)}, actual={sorted(changed)}')
 for resource,e,spec in images:
  tex=next(o for o in check.objects if o.path_id==e['evidence']['texture_path_id']).read();expected_image=_image_rgba(spec['png'],tex.m_Width,tex.m_Height)
  if tex.image.convert('RGBA').tobytes()!=expected_image.tobytes():raise BuildError('texture reload mismatch: '+resource)
 return output,{'changed_object_ids':sorted(changed)}

def _save_isolation(source:bytes)->bytes:
 if sha(source)!='ac30086341952ed0179097dd6988173bf6df137990038340f32d04c41cfa542e':raise BuildError('save-isolation assembly hash mismatch')
 offset=126776;old=struct.pack('<I',0x0A000212);new=struct.pack('<I',0x0A00013C)
 if source[offset:offset+4]!=old:raise BuildError('save-isolation IL site mismatch')
 return source[:offset]+new+source[offset+4:]

def _valid_scratch(path:Path|None)->bool:
 if path is None:return False
 resolved=Path(path).resolve()
 return resolved.is_relative_to(SCRATCH_ROOT.resolve()) or resolved.is_relative_to(Path('/tmp').resolve())

def build_full(baseline:Path,baseline_manifest:Any,translations:Mapping[str,str]|Iterable[Mapping[str,Any]],*,mode:str='structural',provenance:dict[str,Any]|None=None,reviews:Mapping[str,bytes]|None=None,image_assets:Mapping[str,dict[str,Any]]|None=None,font_bytes:bytes|None=None,save_policy:str='persistent_user_data',scratch_dir:Path|None=None,expected_baseline_files:int=1684)->tuple[dict[str,bytes],dict[str,Any]]:
 full=load_full_corpus();values=normalize_translations(full,translations,mode,provenance,reviews);base=verify_baseline(baseline,baseline_manifest,expected_baseline_files)
 core=_module('stage8_core_adapter',ROOT/'work/stage3/adapter/game_patch.py');extra=_module('stage8_extra_adapter',ROOT/'work/stage3/extra_adapter/adapter.py');stat_display=_module('stage8_stat_display',ROOT/'work/stage8/build/stat_display.py');cp=_core_projection(full,core);core_values={i:values[i] for i in cp.by_id};core.normalize_translations(core_values,cp)
 patches=defaultdict(list)
 for i,s in cp.by_id.items():
  e=cp.sidecar[i];start,end=e['token_byte_span'] if e['container_kind']=='loose_yaml' else e['token_byte_span_in_script'];raw=core._yaml_raw(core_values[i],s['source']['text'],e['raw_token']) if e['container_kind']=='loose_yaml' else core._textasset_raw(core_values[i],s['source']['text'],e['raw_token']);patches[e['container']].append((start,end,raw.encode(),i))
 candidates={}
 for rel,ps in patches.items():
  candidates[rel]=core._patch_resources(base[rel],ps,cp,font_bytes)[0] if rel==core.RESOURCE_PATH else core._replace_spans(base[rel],ps)
 facts=_extra_facts(full);extra.normalize_translations({i:values[i] for i in facts},facts);by_container=defaultdict(list);il=[]
 for f in facts.values():
  (il if f['locator']['kind']=='managed_il_ldstr' else by_container[DATA+f['locator']['container']]).append(f)
 image_entries={i:e for i,e in full.sidecar.items() if e['container_kind']=='image_asset'};needed={e['resource_id'] for i,e in image_entries.items() if values[i]!=full.by_id[i]['source']['text']};assets=dict(image_assets or {})
 if set(assets)!=needed:raise BuildError(f'image asset coverage mismatch: required={sorted(needed)}, supplied={sorted(assets)}')
 image_jobs=defaultdict(list)
 for resource in needed:
  entries=[e for e in image_entries.values() if e['resource_id']==resource];e=entries[0];spec=assets[resource]
  if resource.startswith('unity:'):image_jobs[DATA+resource.split(':')[1]].append((resource,e,spec))
  else:
   rel='YAML/Images/Newspaper.png';ev=e['evidence']
   if sha(base[rel])!=ev['sha256']:raise BuildError('loose image source hash mismatch')
   source_im=_image_rgba(base[rel],ev['width'],ev['height']);_validate_image(spec,source_im,ev['width'],ev['height']);candidates[rel]=spec['png']
 active_unity={rel for rel,fs in by_container.items() if any(values[f['id']]!=f['source'] for f in fs)}|set(image_jobs)
 for rel in sorted(active_unity):
  candidates[rel],_= _compose_unity(base[rel],Path(baseline)/rel,by_container.get(rel,[]),values,image_jobs.get(rel,[]),extra)
 assembly=base[ASSEMBLY]
 if save_policy=='isolated_data_path_test_only':assembly=_save_isolation(assembly)
 elif save_policy!='persistent_user_data':raise BuildError('unknown save policy')
 if any(values[f['id']]!=f['source'] for f in il):
  if not _valid_scratch(scratch_dir):raise BuildError('changed IL requires approved stage8 or /tmp scratch')
  adjusted=[]
  for f in il:
   x=json.loads(json.dumps(f));x['locator']['assembly_sha256']=sha(assembly);adjusted.append(x)
  assembly,_=extra._patch_il(assembly,adjusted,values,Path(scratch_dir))
 stat_source=full.by_id[stat_display.STAT_UI_ID]['source']['text'];disease_source=full.by_id[stat_display.DISEASE_UI_ID]['source']['text'];stat_active=values[stat_display.STAT_UI_ID]!=stat_source or values[stat_display.DISEASE_UI_ID]!=disease_source
 stat_meta={'applied':False,'byte_identical':True,'labels_reused':list(stat_display.RAW_STATS)}
 if stat_active:
  if not _valid_scratch(scratch_dir):raise BuildError('changed stat display requires approved stage8 or /tmp scratch')
  try:assembly,stat_meta=stat_display.patch_stat_display(assembly,base[ASSEMBLY],stat_source,values[stat_display.STAT_UI_ID],disease_source,values[stat_display.DISEASE_UI_ID],Path(scratch_dir))
  except stat_display.StatDisplayError as exc:raise BuildError(str(exc)) from exc
 if assembly!=base[ASSEMBLY]:candidates[ASSEMBLY]=assembly
 outputs={p:b for p,b in candidates.items() if b!=base[p]}
 rows=[{'path':p,'input_sha256':sha(base[p]),'output_sha256':sha(b),'output_size':len(b)} for p,b in sorted(outputs.items())]
 baseline_index=normalize_baseline_manifest(baseline_manifest)
 meta={'schema_version':'1.0.0','game_version':'steam-build:18387525','source_revision':REVISION,'corpus_hashes':CORPUS_HASHES,'baseline_manifest_sha256':sha(canonical(sorted(baseline_index.items()))),'mode':mode,'approved':mode=='release','translation_provenance':provenance if mode=='release' else None,'save_policy':save_policy,'font_sha256':sha(font_bytes) if font_bytes is not None else None,'image_assets':{k:v['sha256'] for k,v in sorted(assets.items())},'stat_display':stat_meta,'processed_ids':len(full.rows),'changed_ids':sum(values[i]!=full.by_id[i]['source']['text'] for i in values),'baseline_files_verified':len(base),'tmp_diagnostic_excluded':True,'files':rows,'runtime_verified':False}
 meta['build_id']='stage8-'+sha(canonical(meta))[:20]
 return outputs,meta

def write_outputs(output_dir:Path,files:Mapping[str,bytes],manifest:dict[str,Any])->None:
 out=Path(output_dir)
 resolved=out.resolve()
 if any(parent.is_symlink() for parent in (out,*out.parents)):raise BuildError('symlinked output path forbidden')
 if out.exists():raise BuildError('output directory must be new')
 out.mkdir(parents=True)
 try:
  for rel,data in sorted(files.items()):p=out/PurePosixPath(rel);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data)
  (out/'build-manifest.json').write_bytes(canonical(manifest))
 except Exception:
  shutil.rmtree(out,ignore_errors=True);raise
