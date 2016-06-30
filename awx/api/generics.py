# Copyright (c) 2015 Ansible, Inc.
# All Rights Reserved.

# Python
import inspect
import logging
import time

# Django
from django.conf import settings
from django.db import connection
from django.http import QueryDict
from django.shortcuts import get_object_or_404
from django.template.loader import render_to_string
from django.utils.encoding import smart_text
from django.utils.safestring import mark_safe
from django.contrib.contenttypes.models import ContentType

# Django REST Framework
from rest_framework.authentication import get_authorization_header
from rest_framework.exceptions import PermissionDenied
from rest_framework import generics
from rest_framework.response import Response
from rest_framework import status
from rest_framework import views

# AWX
from awx.main.models import *  # noqa
from awx.main.utils import * # noqa
from awx.api.serializers import ResourceAccessListElementSerializer

__all__ = ['APIView', 'GenericAPIView', 'ListAPIView', 'SimpleListAPIView',
           'ListCreateAPIView', 'SubListAPIView', 'SubListCreateAPIView',
           'SubListCreateAttachDetachAPIView', 'RetrieveAPIView',
           'RetrieveUpdateAPIView', 'RetrieveDestroyAPIView',
           'RetrieveUpdateDestroyAPIView', 'DestroyAPIView',
           'SubDetailAPIView',
           'ResourceAccessList',
           'ParentMixin',
           'DeleteLastUnattachLabelMixin',]

logger = logging.getLogger('awx.api.generics')

def get_view_name(cls, suffix=None):
    '''
    Wrapper around REST framework get_view_name() to support get_name() method
    and view_name property on a view class.
    '''
    name = ''
    if hasattr(cls, 'get_name') and callable(cls.get_name):
        name = cls().get_name()
    elif hasattr(cls, 'view_name'):
        if callable(cls.view_name):
            name = cls.view_name()
        else:
            name = cls.view_name
    if name:
        return ('%s %s' % (name, suffix)) if suffix else name
    return views.get_view_name(cls, suffix=None)

def get_view_description(cls, html=False):
    '''
    Wrapper around REST framework get_view_description() to support
    get_description() method and view_description property on a view class.
    '''
    if hasattr(cls, 'get_description') and callable(cls.get_description):
        desc = cls().get_description(html=html)
        cls = type(cls.__name__, (object,), {'__doc__': desc})
    elif hasattr(cls, 'view_description'):
        if callable(cls.view_description):
            view_desc = cls.view_description()
        else:
            view_desc = cls.view_description
        cls = type(cls.__name__, (object,), {'__doc__': view_desc})
    desc = views.get_view_description(cls, html=html)
    if html:
        desc = '<div class="description">%s</div>' % desc
    return mark_safe(desc)

class APIView(views.APIView):

    def initialize_request(self, request, *args, **kwargs):
        '''
        Store the Django REST Framework Request object as an attribute on the
        normal Django request, store time the request started.
        '''
        self.time_started = time.time()
        if getattr(settings, 'SQL_DEBUG', False):
            self.queries_before = len(connection.queries)
        drf_request = super(APIView, self).initialize_request(request, *args, **kwargs)
        request.drf_request = drf_request
        return drf_request

    def finalize_response(self, request, response, *args, **kwargs):
        '''
        Log warning for 400 requests.  Add header with elapsed time.
        '''
        if response.status_code >= 400:
            status_msg = "status %s received by user %s attempting to access %s from %s" % \
                         (response.status_code, request.user, request.path, request.META.get('REMOTE_ADDR', None))
            if response.status_code == 401:
                logger.info(status_msg)
            else:
                logger.warn(status_msg)
        response = super(APIView, self).finalize_response(request, response, *args, **kwargs)
        time_started = getattr(self, 'time_started', None)
        if time_started:
            time_elapsed = time.time() - self.time_started
            response['X-API-Time'] = '%0.3fs' % time_elapsed
        if getattr(settings, 'SQL_DEBUG', False):
            queries_before = getattr(self, 'queries_before', 0)
            q_times = [float(q['time']) for q in connection.queries[queries_before:]]
            response['X-API-Query-Count'] = len(q_times)
            response['X-API-Query-Time'] = '%0.3fs' % sum(q_times)
        return response

    def get_authenticate_header(self, request):
        """
        Determine the WWW-Authenticate header to use for 401 responses.  Try to
        use the request header as an indication for which authentication method
        was attempted.
        """
        for authenticator in self.get_authenticators():
            resp_hdr = authenticator.authenticate_header(request)
            if not resp_hdr:
                continue
            req_hdr = get_authorization_header(request)
            if not req_hdr:
                continue
            if resp_hdr.split()[0] and resp_hdr.split()[0] == req_hdr.split()[0]:
                return resp_hdr
        # If it can't be determined from the request, use the last
        # authenticator (should be Basic).
        try:
            return authenticator.authenticate_header(request)
        except NameError:
            pass

    def get_description_context(self):
        return {
            'view': self,
            'docstring': type(self).__doc__ or '',
            'new_in_13': getattr(self, 'new_in_13', False),
            'new_in_14': getattr(self, 'new_in_14', False),
            'new_in_145': getattr(self, 'new_in_145', False),
            'new_in_148': getattr(self, 'new_in_148', False),
            'new_in_200': getattr(self, 'new_in_200', False),
            'new_in_210': getattr(self, 'new_in_210', False),
            'new_in_220': getattr(self, 'new_in_220', False),
            'new_in_230': getattr(self, 'new_in_230', False),
            'new_in_240': getattr(self, 'new_in_240', False),
            'new_in_300': getattr(self, 'new_in_300', False),
        }

    def get_description(self, html=False):
        template_list = []
        for klass in inspect.getmro(type(self)):
            template_basename = camelcase_to_underscore(klass.__name__)
            template_list.append('api/%s.md' % template_basename)
        context = self.get_description_context()
        return render_to_string(template_list, context)

    def update_raw_data(self, data):
        # Remove the parent key if the view is a sublist, since it will be set
        # automatically.
        parent_key = getattr(self, 'parent_key', None)
        if parent_key:
            data.pop(parent_key, None)

        # Use request data as-is when original request is an update and the
        # submitted data was rejected.
        request_method = getattr(self, '_raw_data_request_method', None)
        response_status = getattr(self, '_raw_data_response_status', 0)
        if request_method in ('POST', 'PUT', 'PATCH') and response_status in xrange(400, 500):
            return self.request.data.copy()

        return data


class GenericAPIView(generics.GenericAPIView, APIView):
    # Base class for all model-based views.

    # Subclasses should define:
    #   model = ModelClass
    #   serializer_class = SerializerClass

    def get_serializer(self, *args, **kwargs):
        serializer = super(GenericAPIView, self).get_serializer(*args, **kwargs)
        # Override when called from browsable API to generate raw data form;
        # update serializer "validated" data to be displayed by the raw data
        # form.
        if hasattr(self, '_raw_data_form_marker'):
            # Always remove read only fields from serializer.
            for name, field in serializer.fields.items():
                if getattr(field, 'read_only', None):
                    del serializer.fields[name]
            serializer._data = self.update_raw_data(serializer.data)
        return serializer

    def get_queryset(self):
        #if hasattr(self.request.user, 'get_queryset'):
        #    return self.request.user.get_queryset(self.model)
        if self.queryset is not None:
            return self.queryset._clone()
        elif self.model is not None:
            return self.model._default_manager.all()
        else:
            return super(GenericAPIView, self).get_queryset()

    def get_description_context(self):
        # Set instance attributes needed to get serializer metadata.
        if not hasattr(self, 'request'):
            self.request = None
        if not hasattr(self, 'format_kwarg'):
            self.format_kwarg = 'format'
        d = super(GenericAPIView, self).get_description_context()
        if hasattr(self.model, "_meta"):
            if hasattr(self.model._meta, "verbose_name"):
                d.update({
                    'model_verbose_name': smart_text(self.model._meta.verbose_name),
                    'model_verbose_name_plural': smart_text(self.model._meta.verbose_name_plural),
                })
            d['serializer_fields'] = self.metadata_class().get_serializer_info(self.get_serializer())
        d['settings'] = settings
        return d

class SimpleListAPIView(generics.ListAPIView, GenericAPIView):

    def get_queryset(self):
        return self.request.user.get_queryset(self.model)

class ListAPIView(generics.ListAPIView, GenericAPIView):
    # Base class for a read-only list view.

    def get_queryset(self):
        return self.request.user.get_queryset(self.model)

    def get_description_context(self):
        opts = self.model._meta
        if 'username' in opts.get_all_field_names():
            order_field = 'username'
        else:
            order_field = 'name'
        d = super(ListAPIView, self).get_description_context()
        d.update({
            'order_field': order_field,
        })
        return d

    @property
    def search_fields(self):
        fields = []
        for field in self.model._meta.fields:
            if field.name in ('username', 'first_name', 'last_name', 'email',
                              'name', 'description', 'email'):
                fields.append(field.name)
        return fields

class ListCreateAPIView(ListAPIView, generics.ListCreateAPIView):
    # Base class for a list view that allows creating new objects.
    pass

class ParentMixin(object):

    def get_parent_object(self):
        parent_filter = {
            self.lookup_field: self.kwargs.get(self.lookup_field, None),
        }
        return get_object_or_404(self.parent_model, **parent_filter)

    def check_parent_access(self, parent=None):
        parent = parent or self.get_parent_object()
        parent_access = getattr(self, 'parent_access', 'read')
        if parent_access in ('read', 'delete'):
            args = (self.parent_model, parent_access, parent)
        else:
            args = (self.parent_model, parent_access, parent, None)
        if not self.request.user.can_access(*args):
            raise PermissionDenied()

class SubListAPIView(ListAPIView, ParentMixin):
    # Base class for a read-only sublist view.

    # Subclasses should define at least:
    #   model = ModelClass
    #   serializer_class = SerializerClass
    #   parent_model = ModelClass
    #   relationship = 'rel_name_from_parent_to_model'
    # And optionally (user must have given access permission on parent object
    # to view sublist):
    #   parent_access = 'read'

    def get_description_context(self):
        d = super(SubListAPIView, self).get_description_context()
        d.update({
            'parent_model_verbose_name': smart_text(self.parent_model._meta.verbose_name),
            'parent_model_verbose_name_plural': smart_text(self.parent_model._meta.verbose_name_plural),
        })
        return d

    def get_queryset(self):
        parent = self.get_parent_object()
        self.check_parent_access(parent)
        qs = self.request.user.get_queryset(self.model).distinct()
        sublist_qs = getattrd(parent, self.relationship).distinct()
        return qs & sublist_qs

class SubListCreateAPIView(SubListAPIView, ListCreateAPIView):
    # Base class for a sublist view that allows for creating subobjects
    # associated with the parent object.

    # In addition to SubListAPIView properties, subclasses may define (if the
    # sub_obj requires a foreign key to the parent):
    #   parent_key = 'field_on_model_referring_to_parent'

    def get_description_context(self):
        d = super(SubListCreateAPIView, self).get_description_context()
        d.update({
            'parent_key': getattr(self, 'parent_key', None),
        })
        return d

    def create(self, request, *args, **kwargs):
        # If the object ID was not specified, it probably doesn't exist in the
        # DB yet. We want to see if we can create it.  The URL may choose to
        # inject it's primary key into the object because we are posting to a
        # subcollection. Use all the normal access control mechanisms.

        # Make a copy of the data provided (since it's readonly) in order to
        # inject additional data.
        if hasattr(request.data, 'copy'):
            data = request.data.copy()
        else:
            data = QueryDict('')
            data.update(request.data)

        # add the parent key to the post data using the pk from the URL
        parent_key = getattr(self, 'parent_key', None)
        if parent_key:
            data[parent_key] = self.kwargs['pk']

        # attempt to deserialize the object
        serializer = self.get_serializer(data=data)
        if not serializer.is_valid():
            return Response(serializer.errors,
                            status=status.HTTP_400_BAD_REQUEST)

        # Verify we have permission to add the object as given.
        if not request.user.can_access(self.model, 'add', serializer.initial_data):
            raise PermissionDenied()

        # save the object through the serializer, reload and returned the saved
        # object deserialized
        obj = serializer.save()
        serializer = self.get_serializer(instance=obj)

        headers = {'Location': obj.get_absolute_url()}
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)

class SubListCreateAttachDetachAPIView(SubListCreateAPIView):
    # Base class for a sublist view that allows for creating subobjects and
    # attaching/detaching them from the parent.

    def get_description_context(self):
        d = super(SubListCreateAttachDetachAPIView, self).get_description_context()
        d.update({
            "has_attach": True,
        })
        return d

    def attach(self, request, *args, **kwargs):
        created = False
        parent = self.get_parent_object()
        relationship = getattrd(parent, self.relationship)
        sub_id = request.data.get('id', None)
        data = request.data

        # Create the sub object if an ID is not provided.
        if not sub_id:
            response = self.create(request, *args, **kwargs)
            if response.status_code != status.HTTP_201_CREATED:
                return response
            sub_id = response.data['id']
            data = response.data
            try:
                location = response['Location']
            except KeyError:
                location = None
            created = True

        # Retrive the sub object (whether created or by ID).
        sub = get_object_or_400(self.model, pk=sub_id)

        # Verify we have permission to attach.
        if not request.user.can_access(self.parent_model, 'attach', parent, sub,
                                       self.relationship, data,
                                       skip_sub_obj_read_check=created):
            raise PermissionDenied()

        # Attach the object to the collection.
        if sub not in relationship.all():
            relationship.add(sub)

        if created:
            headers = {}
            if location:
                headers['Location'] = location
            return Response(data, status=status.HTTP_201_CREATED, headers=headers)
        else:
            return Response(status=status.HTTP_204_NO_CONTENT)

    def unattach_validate(self, request):
        sub_id = request.data.get('id', None)
        res = None
        if not sub_id:
            data = dict(msg='"id" is required to disassociate')
            res = Response(data, status=status.HTTP_400_BAD_REQUEST)
        return (sub_id, res)

    def unattach_by_id(self, request, sub_id):
        parent = self.get_parent_object()
        parent_key = getattr(self, 'parent_key', None)
        relationship = getattrd(parent, self.relationship)
        sub = get_object_or_400(self.model, pk=sub_id)

        if not request.user.can_access(self.parent_model, 'unattach', parent,
                                       sub, self.relationship, request.data):
            raise PermissionDenied()

        if parent_key:
            sub.delete()
        else:
            relationship.remove(sub)

        return Response(status=status.HTTP_204_NO_CONTENT)

    def unattach(self, request, *args, **kwargs):
        (sub_id, res) = self.unattach_validate(request)
        if res:
            return res
        return self.unattach_by_id(request, sub_id)

    def post(self, request, *args, **kwargs):
        if not isinstance(request.data, dict):
            return Response('invalid type for post data',
                            status=status.HTTP_400_BAD_REQUEST)
        if 'disassociate' in request.data:
            return self.unattach(request, *args, **kwargs)
        else:
            return self.attach(request, *args, **kwargs)

'''
Models for which you want the last instance to be deleted from the database
when the last disassociate is called should inherit from this class. Further, 
the model should implement is_detached()
'''
class DeleteLastUnattachLabelMixin(object):
    def unattach(self, request, *args, **kwargs):
        (sub_id, res) = super(DeleteLastUnattachLabelMixin, self).unattach_validate(request)
        if res:
            return res

        res = super(DeleteLastUnattachLabelMixin, self).unattach_by_id(request, sub_id)

        obj = self.model.objects.get(id=sub_id)

        if obj.is_detached():
            obj.delete()

        return res

class SubDetailAPIView(generics.RetrieveAPIView, GenericAPIView, ParentMixin):
    pass

class RetrieveAPIView(generics.RetrieveAPIView, GenericAPIView):
    pass

class RetrieveUpdateAPIView(RetrieveAPIView, generics.RetrieveUpdateAPIView):

    def update(self, request, *args, **kwargs):
        self.update_filter(request, *args, **kwargs)
        return super(RetrieveUpdateAPIView, self).update(request, *args, **kwargs)

    def partial_update(self, request, *args, **kwargs):
        self.update_filter(request, *args, **kwargs)
        return super(RetrieveUpdateAPIView, self).partial_update(request, *args, **kwargs)

    def update_filter(self, request, *args, **kwargs):
        ''' scrub any fields the user cannot/should not put/patch, based on user context.  This runs after read-only serialization filtering '''
        pass

class RetrieveDestroyAPIView(RetrieveAPIView, generics.RetrieveDestroyAPIView):

    def destroy(self, request, *args, **kwargs):
        # somewhat lame that delete has to call it's own permissions check
        obj = self.get_object()
        if not request.user.can_access(self.model, 'delete', obj):
            raise PermissionDenied()
        obj.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class RetrieveUpdateDestroyAPIView(RetrieveUpdateAPIView, RetrieveDestroyAPIView):
    pass

class DestroyAPIView(GenericAPIView, generics.DestroyAPIView):
    pass


class ResourceAccessList(ListAPIView):

    serializer_class = ResourceAccessListElementSerializer

    def get_queryset(self):
        self.object_id = self.kwargs['pk']
        resource_model = getattr(self, 'resource_model')
        obj = get_object_or_404(resource_model, pk=self.object_id)

        content_type = ContentType.objects.get_for_model(obj)
        roles = set(Role.objects.filter(content_type=content_type, object_id=obj.id))

        ancestors = set()
        for r in roles:
            ancestors.update(set(r.ancestors.all()))
        return self.request.user.get_queryset(User).filter(roles__in=list(ancestors)).distinct()
