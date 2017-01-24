from django.db import models


class TemplateOption(models.Model):

    label = models.TextField()
    display_name = models.TextField()

    '''
    template should have an arg for custom validators once we determine
    what those should look like
    '''
    template = models.TextField(blank=True)

    help_text = models.TextField(blank=True)
    slug = models.SlugField(unique=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.label
