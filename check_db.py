import os, django
os.environ['DJANGO_SETTINGS_MODULE'] = 'gs_bulk.settings'
django.setup()
from core.models import SiteEntry, SiteBatch
print(f"Total Batches: {SiteBatch.objects.count()}")
print(f"Total Entries: {SiteEntry.objects.count()}")
print()
for e in SiteEntry.objects.all()[:3]:
    print(f"ID: {e.id}")
    print(f"Title: {e.title[:60]}")
    print(f"Content length: {len(e.content)} chars")
    print(f"Slug: {e.slug[:40]}")
    print(f"Status: {e.status}")
    print(f"Content preview: {e.content[:100]}...")
    print("---")
