import os, sys
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
target = sys.argv[1] if len(sys.argv) > 1 else 'test_backup'
if not os.path.exists(target):
    print(f'{target} does not exist')
    sys.exit(0)
for root, dirs, files in os.walk(target):
    level = root.replace(target, '').count(os.sep)
    indent = '  ' * level
    basename = os.path.basename(root) or target
    print(f'{indent}{basename}/')
    for f in sorted(files):
        fp = os.path.join(root, f)
        sz = os.path.getsize(fp)
        print(f'{indent}  {sz:>10}  {f}')
