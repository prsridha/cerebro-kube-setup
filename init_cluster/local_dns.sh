# Install dnsmasq package
yum install -y dnsmasq

# Create the required User and Group
groupadd -r dnsmasq
useradd -r -g dnsmasq dnsmasq

# Create a copy of dnsmasq.conf
mv /etc/dnsmasq.conf /etc/dnsmasq.conf.orig

# Set dnsmasq.conf configuration
cat << EOF > /etc/dnsmasq.conf
# Server Configuration
listen-address=127.0.0.1
port=53
bind-interfaces
user=dnsmasq
group=dnsmasq
pid-file=/var/run/dnsmasq.pid
# Name resolution options
resolv-file=/etc/resolv.dnsmasq
cache-size=500
neg-ttl=60
domain-needed
bogus-priv
EOF

# Populate /etc/resolv.dnsmasq
bash -c "echo 'nameserver ${NAMESERVER}' > /etc/resolv.dnsmasq"

# Enable and Start dnsmasq service
systemctl restart dnsmasq.service
systemctl enable dnsmasq.service

# Configure the default DNS resolver as a fallback option by using the following commands
bash -c "echo 'supersede domain-name-servers 127.0.0.1, 169.254.169.253;' >> /etc/dhcp/dhclient.conf"

# Apply the changes
dhclient

# Test that DNS Cache is working as expected
dig aws.amazon.com