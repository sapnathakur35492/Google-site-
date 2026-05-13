import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'gs_bulk.settings')
django.setup()

from core.models import SiteBatch, SiteEntry

def check():
    batches = SiteBatch.objects.filter(status='processing')
    if not batches:
        print("No batches currently processing.")
        # Check for recently stuck batches
        latest = SiteBatch.objects.all().order_by('-id').first()
        if latest:
            print(f"Latest Batch {latest.id} - Status: {latest.status}")
        return
    
    for b in batches:
        print(f"Batch {b.id} - Status: {b.status} - Action: {b.current_action}")
        pending = b.entries.filter(status='pending').count()
        processing = b.entries.filter(status='processing').count()
        success = b.entries.filter(status='success').count()
        failed = b.entries.filter(status='failed').count()
        print(f"  Entries: {pending} pending, {processing} processing, {success} success, {failed} failed")

if __name__ == "__main__":
    check()
