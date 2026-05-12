from django.contrib import admin
from .models import SiteBatch, SiteEntry

@admin.register(SiteBatch)
class SiteBatchAdmin(admin.ModelAdmin):
    list_display = ('id', 'created_at', 'status', 'total_sites', 'completed_sites', 'failed_sites')
    list_filter = ('status',)

@admin.register(SiteEntry)
class SiteEntryAdmin(admin.ModelAdmin):
    list_display = ('title', 'batch', 'status', 'published_url')
    list_filter = ('status', 'batch')
    search_fields = ('title', 'slug')
