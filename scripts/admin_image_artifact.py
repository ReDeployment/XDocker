"""Export tested local image content and verify it before CI publication. Never pushes."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

ARCHES=('amd64','arm64')
REVISION=re.compile(r'^(?:[a-f0-9]{40}|local-tree-[a-f0-9]{64})$')
DIGEST=re.compile(r'^sha256:[a-f0-9]{64}$')
HASH=re.compile(r'^[a-f0-9]{64}$')
FIELDS={'schema','revision','arch','platform','image','image_id','archive','archive_sha256'}


def tag(revision,arch):
    if not isinstance(revision,str) or not REVISION.fullmatch(revision) or arch not in ARCHES:raise ValueError('Invalid revision or architecture')
    return 'xdocker-admin-ci:sha-'+revision+'-'+arch


def sha256(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):value.update(chunk)
    return value.hexdigest()


def inspect(reference):
    data=json.loads(subprocess.check_output(['docker','image','inspect',reference],text=True))
    if not isinstance(data,list) or len(data)!=1:raise ValueError('Unknown candidate image')
    return data[0]


def verify_image(info,revision,arch,identifier=None):
    revisions=[value.partition('=')[2] for value in info.get('Config',{}).get('Env',[]) if value.startswith('XDOCKER_REVISION=')]
    if revisions!=[revision] or info.get('Os')!='linux' or info.get('Architecture')!=arch:raise ValueError('Candidate revision/platform differs from receipt')
    if not isinstance(info.get('Id'),str) or not DIGEST.fullmatch(info['Id']):raise ValueError('Invalid candidate image ID')
    if identifier and info['Id']!=identifier:raise ValueError('Candidate image ID changed')


def export_image(image,revision,arch,directory):
    if image!=tag(revision,arch):raise ValueError('Unexpected candidate tag')
    info=inspect(image);verify_image(info,revision,arch)
    directory=Path(directory)
    if directory.is_symlink():raise ValueError('Artifact directory symlink refused')
    directory.mkdir(parents=True,exist_ok=True)
    archive=directory/('image-'+arch+'.tar');receipt=directory/('receipt-'+arch+'.json')
    if archive.exists() or archive.is_symlink() or receipt.exists() or receipt.is_symlink():raise ValueError('Existing artifact refused')
    subprocess.run(['docker','image','save','--output',str(archive),image],check=True)
    result={'schema':1,'revision':revision,'arch':arch,'platform':'linux/'+arch,'image':image,'image_id':info['Id'],
            'archive':archive.name,'archive_sha256':sha256(archive)}
    receipt.write_text(json.dumps(result,indent=2)+'\n');receipt.chmod(0o600)
    return result


def verify_archive(directory,revision,arch):
    image=tag(revision,arch);directory=Path(directory)
    file=directory/('receipt-'+arch+'.json');archive=directory/('image-'+arch+'.tar')
    if directory.is_symlink() or file.is_symlink() or archive.is_symlink():raise ValueError('Artifact symlink refused')
    if not file.is_file() or file.stat().st_size>8192 or not archive.is_file():raise ValueError('Incomplete artifact')
    row=json.loads(file.read_text())
    if not isinstance(row,dict) or set(row)!=FIELDS or type(row['schema']) is not int or row['schema']!=1:raise ValueError('Invalid receipt schema')
    expected={'revision':revision,'arch':arch,'platform':'linux/'+arch,'image':image,'archive':archive.name}
    if any(row.get(k)!=v for k,v in expected.items()):raise ValueError('Artifact identity/revision differs')
    if not isinstance(row['image_id'],str) or not DIGEST.fullmatch(row['image_id']):raise ValueError('Invalid image ID')
    if not isinstance(row['archive_sha256'],str) or not HASH.fullmatch(row['archive_sha256']) or sha256(archive)!=row['archive_sha256']:raise ValueError('Artifact checksum differs')
    return row,archive


def load_images(directory,revision):
    # Validate BOTH archives before any load; no partial or tampered candidate may publish.
    verified=[verify_archive(directory,revision,arch) for arch in ARCHES]
    for row,archive in verified:
        subprocess.run(['docker','image','load','--input',str(archive)],check=True,stdout=subprocess.DEVNULL)
        verify_image(inspect(row['image']),revision,row['arch'],row['image_id'])
    return [row for row,_ in verified]


def check_index(file):
    file=Path(file)
    if file.is_symlink() or file.stat().st_size>128*1024:raise ValueError('Invalid index file')
    index=json.loads(file.read_text())
    if not isinstance(index,dict) or type(index.get('schemaVersion')) is not int or index['schemaVersion']!=2:raise ValueError('Invalid index schema')
    manifests=index.get('manifests')
    if not isinstance(manifests,list) or len(manifests)!=2:raise ValueError('Index must contain exactly two tested platforms')
    platforms=[]
    for manifest in manifests:
        platform=manifest.get('platform',{})
        if platform.get('os')!='linux' or platform.get('architecture') not in ARCHES or not DIGEST.fullmatch(manifest.get('digest','')):raise ValueError('Unexpected index platform/digest')
        platforms.append(platform['architecture'])
    if set(platforms)!=set(ARCHES):raise ValueError('Incomplete multi-platform index')


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='action',required=True)
    export=sub.add_parser('export');export.add_argument('--image',required=True);export.add_argument('--revision',required=True);export.add_argument('--arch',choices=ARCHES,required=True);export.add_argument('--directory',required=True)
    load=sub.add_parser('load');load.add_argument('--revision',required=True);load.add_argument('--directory',required=True)
    index=sub.add_parser('check-index');index.add_argument('--index',required=True)
    args=parser.parse_args()
    if args.action=='export':export_image(args.image,args.revision,args.arch,args.directory);print('Tested image exported with revision, platform, image ID and archive checksum.')
    elif args.action=='load':load_images(args.directory,args.revision);print('Both tested images loaded and verified; no rebuild or push.')
    else:check_index(args.index);print('Published index contains linux/amd64 and linux/arm64.')


if __name__=='__main__':
    try:main()
    except (OSError,ValueError,TypeError,subprocess.CalledProcessError):raise SystemExit('Artifact verification failed; publication must not continue.') from None
