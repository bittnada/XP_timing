import numbers


class NameIdMap:
    """Compact name/id mapping with explicit forward and reverse storage.

    The old code stored both name->id and id->name in one dict, which doubled
    dict entries and made len(map)//2 necessary. This class keeps the two
    directions separate while preserving bracket lookup for existing callers.
    """

    def __init__(self, label="name"):
        self.label = label
        self.name_to_id = {}
        self.id_to_name = []

    def __len__(self):
        return len(self.id_to_name)

    def __iter__(self):
        return iter(self.name_to_id)

    def __contains__(self, key):
        if isinstance(key, str):
            return key in self.name_to_id
        if isinstance(key, numbers.Integral):
            idx = int(key)
            return 0 <= idx < len(self.id_to_name)
        return False

    def __getitem__(self, key):
        if isinstance(key, str):
            return self.name_to_id[key]
        if isinstance(key, numbers.Integral):
            return self.id_to_name[int(key)]
        raise KeyError(key)

    def __setitem__(self, key, value):
        if isinstance(key, str) and isinstance(value, numbers.Integral):
            self.add(key, int(value))
            return
        if isinstance(key, numbers.Integral) and isinstance(value, str):
            self.add(value, int(key))
            return
        raise TypeError("NameIdMap only supports name->id or id->name assignments")

    def keys(self):
        # Compatibility for existing code that checks either name or id in keys().
        return list(self.name_to_id.keys()) + list(range(len(self.id_to_name)))

    def items(self):
        return list(self.name_to_id.items()) + [(idx, name) for idx, name in enumerate(self.id_to_name)]

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def add(self, name, preferred_id=None):
        if name in self.name_to_id:
            existing_id = self.name_to_id[name]
            if preferred_id is not None and existing_id != preferred_id:
                raise ValueError(
                    "{} '{}' already has id {}, not {}".format(
                        self.label,
                        name,
                        existing_id,
                        preferred_id,
                    )
                )
            return existing_id

        idx = len(self.id_to_name) if preferred_id is None else int(preferred_id)
        if idx < 0:
            raise ValueError("{} id should be non-negative: {}".format(self.label, idx))
        if idx < len(self.id_to_name):
            raise ValueError("{} id {} already belongs to '{}'".format(self.label, idx, self.id_to_name[idx]))
        if idx != len(self.id_to_name):
            raise ValueError(
                "{} id {} is not contiguous; next id should be {}".format(
                    self.label,
                    idx,
                    len(self.id_to_name),
                )
            )

        self.name_to_id[name] = idx
        self.id_to_name.append(name)
        return idx

    def get_or_create(self, name, preferred_id=None):
        return self.add(name, preferred_id)
