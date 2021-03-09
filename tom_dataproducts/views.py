from io import StringIO
from urllib.parse import urlencode, urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.core.cache.utils import make_template_fragment_key
from django.core.management import call_command
from django.http import HttpResponseRedirect
from django.shortcuts import redirect
from django.urls import reverse, reverse_lazy
from django.utils.safestring import mark_safe
from django.views.generic import View, ListView
from django.views.generic.base import RedirectView
from django.views.generic.detail import DetailView
from django.views.generic.edit import CreateView, DeleteView, FormView
from django_filters.views import FilterView
from guardian.shortcuts import assign_perm, get_objects_for_user

from tom_common.hooks import run_hook
from tom_common.hints import add_hint
from tom_common.mixins import Raise403PermissionRequiredMixin
from tom_dataproducts.models import DataProduct, DataProductGroup, ReducedDatum
from tom_dataproducts.exceptions import InvalidFileFormatException
from tom_dataproducts.forms import AddProductToGroupForm, DataProductUploadForm
from tom_dataproducts.filters import DataProductFilter
from tom_dataproducts.data_processor import run_data_processor
from tom_observations.models import ObservationRecord
from tom_observations.facility import get_service_class


class DataProductSaveView(LoginRequiredMixin, View):
    """
    View that handles saving a ``DataProduct`` generated by an observation. Requires authentication.
    """
    def post(self, request, *args, **kwargs):
        """
        Method that handles POST requests for the ``DataProductSaveView``. Gets the observation facility that created
        the data and saves the selected data products as ``DataProduct`` objects. Redirects to the
        ``ObservationDetailView`` for the specific ``ObservationRecord``.

        :param request: Django POST request object
        :type request: HttpRequest
        """
        service_class = get_service_class(request.POST['facility'])
        observation_record = ObservationRecord.objects.get(pk=kwargs['pk'])
        products = request.POST.getlist('products')
        if not products:
            messages.warning(request, 'No products were saved, please select at least one dataproduct')
        elif products[0] == 'ALL':
            products = service_class().save_data_products(observation_record)
            messages.success(request, 'Saved all available data products')
        else:
            for product in products:
                products = service_class().save_data_products(
                    observation_record,
                    product
                )
                messages.success(
                    request,
                    'Successfully saved: {0}'.format('\n'.join(
                        [str(p) for p in products]
                    ))
                )
        return redirect(reverse(
            'tom_observations:detail',
            kwargs={'pk': observation_record.id})
        )


class DataProductUploadView(LoginRequiredMixin, FormView):
    """
    View that handles manual upload of DataProducts. Requires authentication.
    """
    form_class = DataProductUploadForm

    def get_form(self, *args, **kwargs):
        form = super().get_form(*args, **kwargs)
        if not settings.TARGET_PERMISSIONS_ONLY:
            if self.request.user.is_superuser:
                form.fields['groups'].queryset = Group.objects.all()
            else:
                form.fields['groups'].queryset = self.request.user.groups.all()
        return form

    def form_valid(self, form):
        """
        Runs after ``DataProductUploadForm`` is validated. Saves each ``DataProduct`` and calls ``run_data_processor``
        on each saved file. Redirects to the previous page.
        """
        target = form.cleaned_data['target']
        if not target:
            observation_record = form.cleaned_data['observation_record']
            target = observation_record.target
        else:
            observation_record = None
        dp_type = form.cleaned_data['data_product_type']
        data_product_files = self.request.FILES.getlist('files')
        successful_uploads = []
        for f in data_product_files:
            dp = DataProduct(
                target=target,
                observation_record=observation_record,
                data=f,
                product_id=None,
                data_product_type=dp_type
            )
            dp.save()
            try:
                run_hook('data_product_post_upload', dp)
                reduced_data = run_data_processor(dp)
                if not settings.TARGET_PERMISSIONS_ONLY:
                    for group in form.cleaned_data['groups']:
                        assign_perm('tom_dataproducts.view_dataproduct', group, dp)
                        assign_perm('tom_dataproducts.delete_dataproduct', group, dp)
                        assign_perm('tom_dataproducts.view_reduceddatum', group, reduced_data)
                successful_uploads.append(str(dp))
            except InvalidFileFormatException as iffe:
                ReducedDatum.objects.filter(data_product=dp).delete()
                dp.delete()
                messages.error(
                    self.request,
                    'File format invalid for file {0} -- error was {1}'.format(str(dp), iffe)
                )
            except Exception:
                ReducedDatum.objects.filter(data_product=dp).delete()
                dp.delete()
                messages.error(self.request, 'There was a problem processing your file: {0}'.format(str(dp)))
        if successful_uploads:
            messages.success(
                self.request,
                'Successfully uploaded: {0}'.format('\n'.join([p for p in successful_uploads]))
            )

        return redirect(form.cleaned_data.get('referrer', '/'))

    def form_invalid(self, form):
        """
        Adds errors to Django messaging framework in the case of an invalid form and redirects to the previous page.
        """
        # TODO: Format error messages in a more human-readable way
        messages.error(self.request, 'There was a problem uploading your file: {}'.format(form.errors.as_json()))
        return redirect(form.cleaned_data.get('referrer', '/'))


class DataProductDeleteView(Raise403PermissionRequiredMixin, DeleteView):
    """
    View that handles the deletion of a ``DataProduct``. Requires authentication.
    """
    model = DataProduct
    permission_required = 'tom_dataproducts.delete_dataproduct'
    success_url = reverse_lazy('home')

    def get_required_permissions(self, request=None):
        if settings.TARGET_PERMISSIONS_ONLY:
            return None
        return super(Raise403PermissionRequiredMixin, self).get_required_permissions(request)

    def check_permissions(self, request):
        if settings.TARGET_PERMISSIONS_ONLY:
            return False
        return super(Raise403PermissionRequiredMixin, self).check_permissions(request)

    def get_success_url(self):
        """
        Gets the URL specified in the query params by "next" if it exists, otherwise returns the URL for home.

        :returns: referer or the index URL
        :rtype: str
        """
        referer = self.request.GET.get('next', None)
        referer = urlparse(referer).path if referer else '/'
        return referer

    def delete(self, request, *args, **kwargs):
        """
        Method that handles DELETE requests for this view. First deletes all ``ReducedDatum`` objects associated with
        the ``DataProduct``, then deletes the ``DataProduct``.

        :param request: Django POST request object
        :type request: HttpRequest
        """
        ReducedDatum.objects.filter(data_product=self.get_object()).delete()
        self.get_object().data.delete()
        return super().delete(request, *args, **kwargs)

    def get_context_data(self, *args, **kwargs):
        """
        Adds the referer to the query parameters as "next" and returns the context dictionary.

        :returns: context dictionary
        :rtype: dict
        """
        context = super().get_context_data(*args, **kwargs)
        context['next'] = self.request.META.get('HTTP_REFERER', '/')
        return context


class DataProductListView(FilterView):
    """
    View that handles the list of ``DataProduct`` objects.
    """

    model = DataProduct
    template_name = 'tom_dataproducts/dataproduct_list.html'
    paginate_by = 25
    filterset_class = DataProductFilter
    strict = False

    def get_queryset(self):
        """
        Gets the set of ``DataProduct`` objects that the user has permission to view.

        :returns: Set of ``DataProduct`` objects
        :rtype: QuerySet
        """
        if settings.TARGET_PERMISSIONS_ONLY:
            return super().get_queryset().filter(
                target__in=get_objects_for_user(self.request.user, 'tom_targets.view_target')
            )
        else:
            return get_objects_for_user(self.request.user, 'tom_dataproducts.view_dataproduct')

    def get_context_data(self, *args, **kwargs):
        """
        Adds the set of ``DataProductGroup`` objects to the context dictionary.

        :returns: context dictionary
        :rtype: dict
        """
        context = super().get_context_data(*args, **kwargs)
        context['product_groups'] = DataProductGroup.objects.all()
        return context


class DataProductFeatureView(View):
    """
    View that handles the featuring of ``DataProduct``s. A featured ``DataProduct`` is displayed on the
    ``TargetDetailView``.
    """
    def get(self, request, *args, **kwargs):
        """
        Method that handles the GET requests for this view. Sets all other ``DataProduct``s to unfeatured in the
        database, and sets the specified ``DataProduct`` to featured. Caches the featured image. Deletes previously
        featured images from the cache.
        """
        product_id = kwargs.get('pk', None)
        product = DataProduct.objects.get(pk=product_id)
        try:
            current_featured = DataProduct.objects.filter(
                featured=True,
                data_product_type=product.data_product_type,
                target=product.target
            )
            for featured_image in current_featured:
                featured_image.featured = False
                featured_image.save()
                featured_image_cache_key = make_template_fragment_key(
                    'featured_image',
                    str(featured_image.target.id)
                )
                cache.delete(featured_image_cache_key)
        except DataProduct.DoesNotExist:
            pass
        product.featured = True
        product.save()
        return redirect(reverse(
            'tom_targets:detail',
            kwargs={'pk': request.GET.get('target_id')})
        )


class DataProductGroupDetailView(DetailView):
    """
    View that handles the viewing of a specific ``DataProductGroup``.
    """
    model = DataProductGroup

    def post(self, request, *args, **kwargs):
        """
        Handles the POST request for this view.
        """
        group = self.get_object()
        for product in request.POST.getlist('products'):
            group.dataproduct_set.remove(DataProduct.objects.get(pk=product))
        group.save()
        return redirect(reverse(
            'tom_dataproducts:group-detail',
            kwargs={'pk': group.id})
        )


class DataProductGroupListView(ListView):
    """
    View that handles the display of all ``DataProductGroup`` objects.
    """
    model = DataProductGroup


class DataProductGroupCreateView(LoginRequiredMixin, CreateView):
    """
    View that handles the creation of a new ``DataProductGroup``.
    """
    model = DataProductGroup
    success_url = reverse_lazy('tom_dataproducts:group-list')
    fields = ['name']


class DataProductGroupDeleteView(LoginRequiredMixin, DeleteView):
    """
    View that handles the deletion of a ``DataProductGroup``. Requires authentication.
    """
    success_url = reverse_lazy('tom_dataproducts:group-list')
    model = DataProductGroup


class DataProductGroupDataView(LoginRequiredMixin, FormView):
    """
    View that handles the addition of ``DataProduct``s to a ``DataProductGroup``. Requires authentication.
    """
    form_class = AddProductToGroupForm
    template_name = 'tom_dataproducts/add_product_to_group.html'

    def form_valid(self, form):
        """
        Runs after form validation. Adds the specified ``DataProduct`` objects to the group.

        :param form: Form with data products and group information
        :type form: AddProductToGroupForm
        """
        group = form.cleaned_data['group']
        group.dataproduct_set.add(*form.cleaned_data['products'])
        group.save()
        return redirect(reverse(
            'tom_dataproducts:group-detail',
            kwargs={'pk': group.id})
        )


class UpdateReducedDataView(LoginRequiredMixin, RedirectView):
    """
    View that handles the updating of reduced data tied to a ``DataProduct`` that was automatically ingested from a
    broker. Requires authentication.
    """
    def get(self, request, *args, **kwargs):
        """
        Method that handles the GET requests for this view. Calls the management command to update the reduced data and
        adds a hint using the messages framework about automation.
        """
        # QueryDict is immutable, and we want to append the remaining params to the redirect URL
        query_params = request.GET.copy()
        target_id = query_params.pop('target_id', None)
        out = StringIO()
        if target_id:
            call_command('updatereduceddata', target_id=target_id, stdout=out)
        else:
            call_command('updatereduceddata', stdout=out)
        messages.info(request, out.getvalue())
        add_hint(request, mark_safe(
                          'Did you know updating observation statuses can be automated? Learn how in '
                          '<a href=https://tom-toolkit.readthedocs.io/en/stable/customization/automation.html>'
                          'the docs.</a>'))
        return HttpResponseRedirect(f'{self.get_redirect_url(*args, **kwargs)}?{urlencode(query_params)}')

    def get_redirect_url(self):
        """
        Returns redirect URL as specified in the HTTP_REFERER field of the request.

        :returns: referer
        :rtype: str
        """
        referer = self.request.META.get('HTTP_REFERER', '/')
        return referer
